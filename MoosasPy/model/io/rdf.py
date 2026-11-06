from __future__ import annotations

import json
import os
import re
import warnings
from rdflib import Graph, Namespace, Literal, URIRef
from rdflib.namespace import RDF, RDFS, GEO, BRICK, WGS, XSD

from ..model import *
from ..building_templates import validate_template_snapshot
from ...utils import np, shapely, mixItemListToList, mixItemListToObject, searchBy, generate_code, path
from ...utils.constant import geom

specChar = {" ": "~0~",
            ".": "~1~",
            "/": "~2~",
            "?": "~3~",
            "&": "~4~",
            "=": "~5~",
            ":": "~6~",
            "%": "~7~",
            ">": "~8~",
            "{": "~9~",
            "}": "~10~", }


def _first_or_none(val):
    """Normalize helper results to a single scalar value."""
    if val is None:
        return None
    if isinstance(val, np.ndarray):
        if val.size == 0:
            return None
        return val.flatten()[0]
    if isinstance(val, (list, tuple, set)):
        if len(val) == 0:
            return None
        return list(val)[0]
    return val


def _as_rdf_values(value):
    """Return RDF objects as a list without NumPy coercing a Literal to text."""
    if value is None:
        return []
    if isinstance(value, np.ndarray):
        return list(value.flatten())
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]





def _schedule_value_unit(schedule_name: str) -> str:
    lower = str(schedule_name).lower()
    if "rad" in lower or "solar" in lower:
        return "Wh"
    if "occdens" in lower or "occupantdensity" in lower:
        return "person/m2"
    if "occheat" in lower or "occupantheat" in lower:
        return "W/person"
    if "equip" in lower or "lighting" in lower:
        return "W/m2"
    return "1"


def _interface_surface_type(name: str) -> str:
    lower = str(name).lower()
    if lower in ("exteriorwall", "interiorwall", "roof", "interiorfloor", "shade",
                 "undergroundwall", "undergroundslab", "ceiling", "airwall",
                 "raisedfloor", "slabongrade", "freestandingcolumn", "embeddedcolumn"):
        return str(name)
    return "ExteriorWall"


def _opening_type_from_glazing(glazing) -> str:
    # Category 2 is used by the geometry layer for air boundaries.
    if getattr(glazing, "category", 0) == 2:
        return "AirWindow"
    return "OperableWindow"


def _is_numeric_text(value) -> bool:
    try:
        float(str(value))
        return True
    except Exception:
        return False


def _setting_literal(value):
    """Preserve booleans, numeric strings, arrays and nested settings exactly."""
    from ..metadata import _json_value
    if hasattr(value, 'tolist'):
        value = value.tolist()
    if isinstance(value, (dict, list, tuple)):
        return Literal(json.dumps(value, default=_json_value, allow_nan=False),
                       datatype=URIRef('urn:moosas:json'))
    return Literal(value, datatype=XSD.string) if isinstance(value, str) else Literal(value)


def _literal_to_python(value):
    if value is None:
        return None
    if isinstance(value, Literal):
        if value.datatype == XSD.string:
            return str(value)
        if str(value.datatype) == 'urn:moosas:json':
            return json.loads(str(value))
        value = value.toPython()
    if isinstance(value, URIRef):
        text = str(value).strip()
        text = text.rsplit("#", 1)[-1]
        text = text.rsplit("/", 1)[-1]
        return text
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return ""
        if _is_numeric_text(text):
            num = float(text)
            return int(num) if num.is_integer() else num
        return text
    return value


def _decode_space_setting_value(value):
    value = _literal_to_python(value)
    if isinstance(value, str):
        text = value.strip()
        if ("://" in text or text.startswith("file:")) and "/" in text:
            tail = text.rsplit("#", 1)[-1]
            tail = tail.rsplit("/", 1)[-1]
            if tail:
                return tail
        if _is_numeric_text(text):
            num = float(text)
            return int(num) if num.is_integer() else num
        elif text.lower() in ("true", "false"):
            return text.lower() == "true"
        elif text.lower() in ("none", "null"):
            return None
        return text
    return value


def encodeURI(hint):
    """
    Encode a string into a URI by replacing spaces with underscores and converting to a URIRef object.

    Parameters
    ----------
    hint : str
        The input string to be encoded into a URI. It will be stripped of leading/trailing whitespace
        and have spaces replaced with underscores.

    Returns
    -------
    rdflib.term.URIRef
        A URIRef object created from the processed hint string.

    Raises
    ------
    Exception
        If the input string contains an exclamation mark ('!').
    """
    for key, val in specChar.items():
        escaped_key = re.escape(key)
        hint = re.sub(escaped_key, val, str(hint).strip())
    if "!" in hint:
        raise Exception
    return URIRef(hint)


def decodeURI(hint):
    hint = str(hint).strip()
    for key, val in specChar.items():
        hint = re.sub(val, key, hint)
    return hint


def _parse_nt_lines(graph, path):
    """Standard RDFLib token parser with linear universal-newline input.

    RDFLib's chunked readline repeatedly rescans a growing literal. Native
    thermal metadata can have multi-megabyte JSON lines; TextIO handles LF,
    CRLF and CR without rescanning those prefixes.
    """
    from rdflib.plugins.parsers.ntriples import W3CNTriplesParser, NTGraphSink
    parser = W3CNTriplesParser(NTGraphSink(graph))
    with open(path, 'r', encoding='utf-8', newline=None) as stream:
        for line in stream:
            parser.line = line.rstrip('\r\n')
            parser.parseline()
    return graph


class MoosasRDF(Graph):
    def __init__(self, model: MoosasModel = None, dumpUseless=True, ExportIFC=False):
        """
        Initialize the MoosasRDF instance with optional model encoding and namespace bindings.
        
        Parameters
        ----------
        model : MoosasModel, optional
            The model to encode into the graph. If provided, the model is encoded using the `encodeModel` method.
            Default is None.
        dumpUseless : bool, default True
            If True, useless or redundant information is excluded during model encoding. 
            This parameter is passed to the `encodeModel` method.
        ExportIFC : bool, default False
            If True, enables IFC-specific export features during model encoding.
            This parameter is passed to the `encodeModel` method.
        
        Returns
        -------
        None
        """
        super(MoosasRDF, self).__init__()
        # create namespace
        self.bot = Namespace("https://w3id.org/bot#")
        self.moosas = Namespace("https://moosas#")
        self.pgd = Namespace("http://www.hkust.edu.hk/zhaojiwu/performance_based_generative_design#")
        self.ifc = Namespace("http://www.buildingsmart-tech.org/mvd/IFC4Add1/DTV/1.0/html/")
        self.idfNameSpace = 'https://energyplus.net#'
        self.idf = Namespace('https://energyplus.net#')
        self.rdf = RDF
        self.rdfs = RDFS
        self.geo = GEO
        self.brick = BRICK
        self.wgs = WGS
        self.model = model

        self.bind("idf", self.idf)
        self.bind("moosas", self.moosas)
        self.bind("bot", self.bot)
        self.bind("bes", self.pgd)
        self.bind("rdf", RDF)
        self.bind("rdfs", RDFS)
        self.bind("geo", GEO)
        self.bind("brick", BRICK)
        self.bind("wgs", WGS)
        self.bind("ifc", self.ifc)
        bld = URIRef("Building" + generate_code(4))
        self.add((URIRef("Site"), self.rdf.type, self.bot.Site))
        self.add((URIRef("Site"), self.bot.hasBuilding, bld))
        self.add((bld, self.rdf.type, self.bot.Building))
        if model:
            model.autoDescribe()
            self.encodeModel(model, dumpUseless, ExportIFC)

    @classmethod
    def load(cls, filePath, fileFormat='turtle') -> MoosasRDF:
        """
        Load a graph from a file.
        
        Parameters
        ----------
        filePath : str
            Path to the file containing the serialized graph.
        fileFormat : str, optional
            Serialization format of the file (e.g., 'turtle', 'xml', 'n3'). Default is 'turtle'.
        
        Returns
        -------
        rdflib.Graph
            A new instance of the class populated with the parsed data.
        """
        g = cls()
        # Native normalized files are often line-based N-Triples, which is a
        # Turtle subset. Its streaming parser avoids the Turtle parser's very
        # large token cost for provenance/thermal JSON literals. Try only for
        # a full-IRI header; ordinary prefixed Turtle keeps its usual path.
        nt_candidate = False
        if fileFormat == 'turtle':
            with open(filePath, 'rb') as stream:
                nt_candidate = stream.read(4096).lstrip().startswith(b'<')
        if nt_candidate:
            from rdflib.exceptions import ParserError
            try:
                _parse_nt_lines(g, filePath)
                return g
            except (ParserError, ValueError):
                # Full-IRI Turtle can still contain relative IRIs, semicolons
                # or other Turtle syntax. Discard the partial graph on fallback.
                g = cls()
        g.parse(filePath, format=fileFormat)
        return g

    def encodeModel(self, model: MoosasModel, dumpUseless=True, ExportIFC=False):
        """
        Encode a MoosasModel into the ontology representation.
        
        Parameters
        ----------
        model : MoosasModel
            The model to be encoded, containing building elements, geometry, spaces, and other data.
        dumpUseless : bool, optional
            If True, retrieves all faces including those marked as useless; otherwise, uses only specific element lists.
            Default is True.
        ExportIFC : bool, optional
            If True, enables IFC-specific export logic during encoding. Default is False.
        
        Returns
        -------
        None
            This function does not return any value.
        """
        from ..metadata import encode_energy_metadata
        self.add((URIRef('urn:moosas:energy-model'), URIRef('urn:moosas:energy-metadata'),
                  Literal(encode_energy_metadata(model, include_settings=False))))
        self.buildOntology(model)
        self.encodeScheduleOntology()
        self.encodeSchedule(model)
        from .gbxml.energy import encode
        encode(model, self)
        self.encodeStorey(model)
        spaces = []
        self.skipped_spaces = []
        for space in model.spaceList + model.voidList:
            # A void is intentionally allowed to have no floor or ceiling.  Its
            # boundary topology is still needed by downstream model viewers.
            if self._space_has_complete_topology(space) or self._space_has_void_topology(space):
                spaces.append(space)
            else:
                self.skipped_spaces.append(str(getattr(space, "id", "")))
        for space_id in self.skipped_spaces:
            print(f"RDF: skipped incomplete space {space_id}")

        # Old in-memory models may predate editable construction metadata.
        _apply_element_parameter_defaults(model)

        valid_uids = set()
        valid_geometry_ids = set()
        for space in spaces:
            for element in self._space_elements(space):
                valid_uids.add(str(element.Uid))
                for face_id in mixItemListToList(element.faceId):
                    valid_geometry_ids.add(str(face_id))
                for glazing in mixItemListToList(getattr(element, "glazingElement", [])):
                    valid_uids.add(str(glazing.Uid))
                    for face_id in mixItemListToList(glazing.faceId):
                        valid_geometry_ids.add(str(face_id))
        for shading in model.shadingList:
            for face_id in mixItemListToList(shading.faceId):
                valid_geometry_ids.add(str(face_id))

        for geo in model.geometryList:
            if str(geo.faceId) in valid_geometry_ids:
                self.encodeGeo(geo)
        for space in spaces:
            self.encodeSpace(space, ExportIFC)
        

        mElements = {
            "MoosasFace": {element for element in model.faceList if str(element.Uid) in valid_uids},
            "MoosasWall": {element for element in model.wallList if str(element.Uid) in valid_uids},
            "MoosasSkylight": {element for element in model.skylightList if str(element.Uid) in valid_uids},
            "MoosasGlazing": {element for element in model.glazingList if str(element.Uid) in valid_uids},
        }
        uidSet = mElements['MoosasFace'] | mElements['MoosasWall'] | mElements['MoosasSkylight'] | mElements[
            'MoosasGlazing']
        uidSet = [ele.Uid for ele in uidSet]

        for face in mElements['MoosasFace']:
            self.encodeElement(face, "Face", uidSet, ExportIFC)
        for face in mElements['MoosasWall']:
            self.encodeElement(face, "AirWall" if face.category == 2 else "Wall", uidSet, ExportIFC)
        for face in mElements['MoosasGlazing']:
            self.encodeElement(face, "Glazing", uidSet, ExportIFC)
        for face in mElements['MoosasSkylight']:
            if face.category == 2:
                self.encodeElement(face, "AirSkylight", uidSet, ExportIFC)
            else:
                self.encodeElement(face, "Skylight", uidSet, ExportIFC)
        for shading in model.shadingList:
            shading_uri = URIRef(f"element_{shading.Uid}")
            self.add((shading_uri, self.rdf.type, self.bot.Element))
            self.add((shading_uri, self.moosas.Uid, Literal(shading.Uid)))
            self.add((shading_uri, self.pgd.hasSurfaceType, self.moosas.Shading))
            for face_id in mixItemListToList(shading.faceId):
                self.add((shading_uri, self.moosas.hasFace, URIRef(str(face_id))))
            for key, value in shading.settings.items():
                self.add((shading_uri, self.moosas.hasSetting, Literal(key)))
                self.add((shading_uri, self.moosas[key], _setting_literal(value)))
            for key in shading.explicit_settings:
                self.add((shading_uri, self.moosas.explicitSetting, Literal(key)))


    @staticmethod
    def _space_has_complete_topology(space):
        floor = getattr(space, "floor", None)
        ceiling = getattr(space, "ceiling", None)
        edge = getattr(space, "edge", None)
        if floor is None or ceiling is None or edge is None:
            return False
        floor_faces = mixItemListToList(getattr(floor, "face", []))
        ceiling_faces = mixItemListToList(getattr(ceiling, "face", []))
        edge_walls = mixItemListToList(getattr(edge, "wall", []))
        return len(floor_faces) > 0 and len(ceiling_faces) > 0 and len(edge_walls) > 0

    @staticmethod
    def _space_is_void(space):
        """Return the transform model's semantic void flag safely."""
        try:
            return bool(space.is_void())
        except (AttributeError, TypeError):
            return False

    @classmethod
    def _space_has_void_topology(cls, space):
        """A void is serializable when it has at least one boundary wall.

        Floor and ceiling are optional for voids, but an empty void would not
        provide anything that the SketchUp-side adapter could reconstruct.
        """
        if not cls._space_is_void(space):
            return False
        edge = getattr(space, "edge", None)
        edge_walls = mixItemListToList(getattr(edge, "wall", [])) if edge is not None else []
        return len(edge_walls) > 0

    @staticmethod
    def _space_elements(space):
        elements = []
        for topology in (getattr(space, "floor", None), getattr(space, "ceiling", None)):
            if topology is not None:
                elements.extend(mixItemListToList(getattr(topology, "face", [])))
        edge = getattr(space, "edge", None)
        if edge is not None:
            elements.extend(mixItemListToList(getattr(edge, "wall", [])))
        result = []
        for element in elements:
            result.append(element)
            result.extend(mixItemListToList(getattr(element, "glazingElement", [])))
        return result

    def encodeScheduleOntology(self):
        self.add((self.pgd.Schedule, self.rdf.type, self.rdfs.Class))
        self.add((self.pgd.DailySchedule, self.rdf.type, self.rdfs.Class))
        self.add((self.pgd.WeeklySchedule, self.rdf.type, self.rdfs.Class))
        self.add((self.pgd.GbxmlSurfaceType, self.rdf.type, self.rdfs.Class))
        self.add((self.pgd.openingType, self.rdf.type, self.rdfs.Class))
        self.add((self.pgd.DailySchedule, self.rdfs.subClassOf, self.pgd.Schedule))
        self.add((self.pgd.WeeklySchedule, self.rdfs.subClassOf, self.pgd.Schedule))
        for predicate in [
            self.pgd.scheduleName,
            self.pgd.timeStepHours,
            self.pgd.valueCount,
            self.pgd.valueUnit,
            self.pgd.valueMode,
            self.pgd.hourlyValuesJson,
            self.pgd.surfaceType,
            self.pgd.mondaySchedule,
            self.pgd.tuesdaySchedule,
            self.pgd.wednesdaySchedule,
            self.pgd.thursdaySchedule,
            self.pgd.fridaySchedule,
            self.pgd.saturdaySchedule,
            self.pgd.sundaySchedule,
        ]:
            self.add((predicate, self.rdf.type, self.rdf.Property))

        for enum_name in [
            "ExteriorWall",
            "InteriorWall",
            "Roof",
            "InteriorFloor",
            "Shade",
            "UndergroundWall",
            "UndergroundSlab",
            "Ceiling",
            "AirWall",
            "RaisedFloor",
            "SlabOnGrade",
            "FreestandingColumn",
            "EmbeddedColumn",
            "OperableWindow",
            "AirWindow",
        ]:
            enum_uri = getattr(self.pgd, enum_name)
            self.add((enum_uri, self.rdf.type, self.pgd.GbxmlSurfaceType if enum_name not in ("OperableWindow", "AirWindow") else self.pgd.openingType))

    def encodeSchedule(self, model: MoosasModel):
        schedule_map = getattr(model, "schedule", {}) or {}
        for scheduleName, scheduleItem in schedule_map.items():
            if not isinstance(scheduleItem, dict):
                continue
            scheduleType = str(scheduleItem.get("type", "")).strip().lower()
            value = scheduleItem.get("value", [])
            scheduleUri = URIRef(str(scheduleName))
            if scheduleType == "daily":
                values = [float(v) if _is_numeric_text(v) else v for v in value]
                self.add((scheduleUri, self.rdf.type, self.pgd.DailySchedule))
                self.add((scheduleUri, self.pgd.scheduleName, Literal(str(scheduleName))))
                self.add((scheduleUri, self.pgd.timeStepHours, Literal(1)))
                self.add((scheduleUri, self.pgd.valueCount, Literal(len(values))))
                self.add((scheduleUri, self.pgd.valueUnit, Literal(_schedule_value_unit(scheduleName))))
                self.add((scheduleUri, self.pgd.valueMode, self.pgd.AbsoluteValue))
                self.add((scheduleUri, self.pgd.hourlyValuesJson, Literal(json.dumps(values))))
            elif scheduleType == "weekly":
                values = [str(v) for v in value]
                if len(values) != 7:
                    continue
                self.add((scheduleUri, self.rdf.type, self.pgd.WeeklySchedule))
                self.add((scheduleUri, self.pgd.scheduleName, Literal(str(scheduleName))))
                day_props = [
                    self.pgd.mondaySchedule,
                    self.pgd.tuesdaySchedule,
                    self.pgd.wednesdaySchedule,
                    self.pgd.thursdaySchedule,
                    self.pgd.fridaySchedule,
                    self.pgd.saturdaySchedule,
                    self.pgd.sundaySchedule,
                ]
                for prop, daily_name in zip(day_props, values):
                    self.add((scheduleUri, prop, URIRef(str(daily_name))))

    def buildOntology(self, model: MoosasModel):
        """
        Constructs an ontology hierarchy for classes in Moosas based on the provided model.
        
        Parameters
        ----------
        model : MoosasModel
            The input model containing building templates and other information used to construct 
            the ontology. The model's `buildingTemplate` attribute is accessed to extract zone 
            information, which is used to define properties and relationships in the ontology.
        
        Returns
        -------
        None
            This function modifies the internal state of the object by adding RDF triples to represent 
            the ontology but does not return any value.
        """
        """constructing hierarchy for class in moosas
        """
        # program
        # geometry
        self.add((self.geo.hasGeometry, self.rdfs.subPropertyOf, self.moosas.Geometry))
        self.add((self.moosas.Category, self.rdfs.subPropertyOf, self.moosas.Geometry))
        self.add((self.moosas.hasHole, self.rdfs.subPropertyOf, self.moosas.Geometry))
        self.add((self.moosas.hasHole, self.rdfs.range, self.geo.Geometry))

        # face Element
        self.add((self.moosas.Uid, self.rdfs.subPropertyOf, self.bot.Element))
        self.add((self.moosas.Offset, self.rdfs.subPropertyOf, self.bot.Element))
        self.add((self.moosas.U_Value, self.rdfs.subPropertyOf, self.bot.Element))
        self.add((self.moosas.SHGC, self.rdfs.subPropertyOf, self.bot.Element))
        self.add((self.moosas.hasNeighborElement, self.rdfs.subClassOf, self.bot.hasSubElement))
        # self.add((self.moosas.hasNeighborElement, self.rdfs.range, self.bot.Element))
        self.add((self.pgd.hasSurfaceType, self.rdfs.range, self.moosas.surfaceType))
        self.add((self.moosas.rawElement, self.rdf.type, self.moosas.surfaceType))
        self.add((self.moosas.rawElement, self.rdfs.comment, Literal(f"Unclassified element")))
        self.add((self.moosas.Wall, self.rdf.type, self.moosas.surfaceType))
        self.add((self.moosas.Wall, self.rdfs.comment,
                  Literal(
                      f"Opaque vertical Element the dot value with (0,0,1) bigger than {geom.HORIZONTAL_ANGLE_THRESHOLD}")))
        self.add((self.moosas.Face, self.rdf.type, self.moosas.surfaceType))
        self.add((self.moosas.Face, self.rdfs.comment, Literal(
            f"Opaque horizontal Element the dot value with (0,0,1) smaller than {geom.HORIZONTAL_ANGLE_THRESHOLD}")))
        self.add((self.moosas.AirWall, self.rdf.type, self.moosas.surfaceType))
        self.add((self.moosas.AirWall, self.rdfs.comment,
                  Literal(
                      f"air boundary Element the dot value with (0,0,1) bigger than {geom.HORIZONTAL_ANGLE_THRESHOLD}")))
        self.add((self.moosas.AirSkylight, self.rdf.type, self.moosas.surfaceType))
        self.add((self.moosas.AirSkylight, self.rdfs.comment,
                  Literal(
                      f"air boundary Element the dot value with (0,0,1) smaller than {geom.HORIZONTAL_ANGLE_THRESHOLD}")))
        self.add((self.moosas.Glazing, self.rdf.type, self.moosas.surfaceType))
        self.add((self.moosas.Glazing, self.rdfs.comment, Literal(
            f"Transparent vertical Element the dot value with (0,0,1) bigger than {geom.HORIZONTAL_ANGLE_THRESHOLD}")))
        self.add((self.moosas.Skylight, self.rdf.type, self.moosas.surfaceType))
        self.add((self.moosas.Skylight, self.rdfs.comment, Literal(
            f"Transparent horizontal Element the dot value with (0,0,1) smaller than {geom.HORIZONTAL_ANGLE_THRESHOLD}")))

        self.add((self.moosas.hasLevel, self.rdfs.subPropertyOf, self.bot.Element))
        self.add((self.moosas.hasLevel, self.rdfs.range, self.bot.Storey))
        self.add((self.moosas.hasFace, self.rdfs.subPropertyOf, self.bot.Element))
        self.add((self.moosas.hasFace, self.rdfs.range, self.moosas.Geometry))

        # space
        self.add((self.moosas.Uid, self.rdfs.subPropertyOf, self.bot.Space))
        self.add((self.moosas.TopoElement, self.rdfs.subClassOf, self.bot.Element))
        self.add((self.moosas.TopoElement, self.rdfs.comment,
                  Literal(
                      f"1LSB element of the space which used to show the topology")))
        self.add((self.moosas.Ceiling, self.rdf.type, self.moosas.surfaceType))
        self.add((self.moosas.Ceiling, self.rdfs.comment,
                  Literal(
                      f"1LSB of the space, composed by multi horizontal faces cap the 99% of area of the top projection space")))
        self.add((self.moosas.Edge, self.rdf.type, self.moosas.surfaceType))
        self.add((self.moosas.Edge, self.rdfs.comment,
                  Literal(
                      f"1LSB of the space, composed by ordered vertical walls in a close loop")))
        self.add((self.moosas.Floor, self.rdf.type, self.moosas.surfaceType))
        self.add((self.moosas.Floor, self.rdfs.comment,
                  Literal(
                      f"1LSB of the space, composed by multi horizontal faces cap the 99% of area of the bottom projection space")))
        self.add((self.moosas.subElementOrder, self.rdfs.subPropertyOf, self.bot.Element))
        self.add((self.moosas.subElementOrder, self.rdfs.comment,
                  Literal(f"the loop order of the sub elements if supported")))

        # level
        self.add((self.moosas.altitute, self.rdfs.subPropertyOf, self.bot.Storey))

    def ifcOntology(self):
        """
        Add IFC4.0 ontology definitions to the current graph for data coupling and semantic interoperability.
        
        Parameters
        ----------
        self : object
            The instance of the class containing namespaces (ifc, rdfs, moosas) and an `add` method 
            for adding RDF triples. It is assumed that this object has attributes `ifc`, `rdfs`, 
            `moosas`, and a method `add(triple)` that accepts an RDF triple.
        
        Returns
        -------
        None
            This function does not return any value. It modifies the state of the instance by adding 
            RDF triples representing IFC4.0 ontology elements and their relationships.
        """
        # adding ifc definition for future data coupling and transition
        self.add((self.ifc.IfcExternalSpatialElement, self.rdfs.comment,
                  Literal(f"representing external of the building according to IFC4.0")))
        self.add(
            (self.ifc.IfcSpace, self.rdfs.comment, Literal(f"representing spaces of the building according to IFC4.0")))
        self.add((self.moosas.refSpace, self.rdfs.subPropertyOf, self.ifc.IfcSpace))
        self.add((self.moosas.refSpace, self.rdfs.range, self.bot.Space))

        self.add(
            (self.ifc.IfcBuildingElement, self.rdfs.comment, Literal(f"elements of the building according to IFC4.0")))
        self.add((self.ifc.GlobalID, self.rdfs.subPropertyOf, self.ifc.IfcBuildingElement))
        self.add((self.moosas.refElement, self.rdfs.subPropertyOf, self.ifc.IfcBuildingElement))
        self.add((self.moosas.refElement, self.rdfs.range, self.bot.Elemetnt))

        self.add((self.ifc.IfcWall, self.rdfs.subClassOf, self.ifc.IfcBuildingElement))
        self.add((self.ifc.IfcCurtainWall, self.rdfs.subClassOf, self.ifc.IfcBuildingElement))
        self.add((self.ifc.IfcWindow, self.rdfs.subClassOf, self.ifc.IfcBuildingElement))
        self.add((self.ifc.IfcRoof, self.rdfs.subClassOf, self.ifc.IfcBuildingElement))
        self.add((self.ifc.IfcSlab, self.rdfs.subClassOf, self.ifc.IfcBuildingElement))
        self.add((self.ifc.IfcVirtualElement, self.rdfs.subClassOf, self.ifc.IfcBuildingElement))

        self.add(
            (self.ifc.IfcRelSpaceBoundary2ndLevel, self.rdfs.comment, Literal(f"2LSB element according to IFC4.0")))
        self.add((self.ifc.GlobalID, self.rdfs.subPropertyOf, self.ifc.IfcRelSpaceBoundary2ndLevel))
        # 2a: heat transfer face; 2b: adiabatic face; shading: shading
        self.add((self.ifc.Description, self.rdfs.subPropertyOf, self.ifc.t))
        # reference to the Space
        self.add((self.ifc.RelatingSpace, self.rdfs.subPropertyOf, self.ifc.IfcRelSpaceBoundary2ndLevel))
        self.add((self.ifc.RelatingSpace, self.rdfs.range, self.ifc.IfcSpace))
        # reference to the buildingElement
        self.add((self.ifc.RelatedBuildingElement, self.rdfs.subPropertyOf, self.ifc.IfcRelSpaceBoundary2ndLevel))
        self.add((self.ifc.RelatedBuildingElement, self.rdfs.range, self.ifc.IfcBuildingElement))
        # reference to the moosas.geometry
        self.add((self.ifc.connectionGeometry, self.rdfs.subPropertyOf, self.ifc.IfcRelSpaceBoundary2ndLevel))
        self.add((self.ifc.connectionGeometry, self.rdfs.range, self.moosas.Geometry))
        # Whether an air boundary
        self.add((self.ifc.PhysicalOrVirtualBoundary, self.rdfs.subPropertyOf, self.ifc.IfcRelSpaceBoundary2ndLevel))
        # in or out surface
        self.add((self.ifc.InternalOrExternalBoundary, self.rdfs.subPropertyOf, self.ifc.IfcRelSpaceBoundary2ndLevel))
        # if this is a window it should have parent boundary
        self.add((self.ifc.ParentBoundary, self.rdfs.subPropertyOf, self.ifc.IfcRelSpaceBoundary2ndLevel))
        self.add((self.ifc.ParentBoundary, self.rdfs.range, self.ifc.IfcRelSpaceBoundary2ndLevel))
        # the boundary object on the other side
        self.add((self.ifc.CorrespondingBoundary, self.rdfs.subPropertyOf, self.ifc.IfcRelSpaceBoundary2ndLevel))
        self.add((self.ifc.CorrespondingBoundary, self.rdfs.range, self.ifc.IfcRelSpaceBoundary2ndLevel))

    def encodeGeo(self, geo: MoosasGeometry):
        """
        Encode a geometric object into RDF triples.
        
        Parameters
        ----------
        geo : MoosasGeometry
            The geometric object to encode, containing attributes such as faceId, category, 
            boundary, and holes. The object is converted into RDF triples representing 
            its properties and geometry in WKT format.
        
        Returns
        -------
        None
            This function does not return a value. It modifies the internal state by adding 
            RDF triples to the instance.
        """
        self.add((URIRef(geo.faceId), self.rdf.type, self.moosas.Geometry))
        self.add((URIRef(geo.faceId), self.moosas.Category, Literal(geo.category)))
        self.add((URIRef(geo.faceId), self.moosas.faceId, Literal(geo.faceId)))
        self.add((URIRef(geo.faceId), self.geo.hasGeometry, URIRef(geo.faceId + "fv")))
        self.add((URIRef(geo.faceId + "fv"), self.geo.asWKT,
                  Literal(shapely.to_wkt(shapely.polygons(geo.boundary),rounding_precision=-1), datatype=self.geo.wktLiteral)))
        if len(geo.holes) > 0:
            for hi, hole in enumerate(geo.holes):
                self.add((URIRef(geo.faceId), self.moosas.hasHole, URIRef(geo.faceId + f"fh{hi}")))
                self.add((URIRef(geo.faceId + f"fh{hi}"), self.geo.asWKT,
                          Literal(shapely.to_wkt(shapely.polygons(hole),rounding_precision=-1), datatype=self.geo.wktLiteral)))

    def encodeElement(self, Element: MoosasElement, typeName: str = "rawElement", mask=None, ExportIFC=False):
        """
        Encode a MoosasElement into RDF triples within the graph.
        
        Parameters
        ----------
        Element : MoosasElement
            The element to be encoded, containing properties such as Uid, offset, area, normal, etc.
        typeName : str, optional
            The type name of the element (e.g., 'rawElement', 'Wall', 'Glazing'), used to assign semantic type. Default is "rawElement".
        mask : set or list, optional
            A collection of neighbor element identifiers to filter which neighbors are added. If provided, only neighbors in the mask are included. Default is None.
        ExportIFC : bool, optional
            If True, generates IFC-compliant RDF triples for the element, including GlobalID and corresponding IFC types. Default is False.
        
        Returns
        -------
        None
            This function does not return a value. It modifies the internal RDF graph by adding triples.
        """
        self.add((URIRef(f"element_{Element.Uid}"), self.rdf.type, self.bot.Element))
        self.add((URIRef(f"element_{Element.Uid}"), self.rdfs.comment, Literal(Element.description)))
        self.add((URIRef(f"element_{Element.Uid}"), self.moosas.Uid, Literal(Element.Uid)))
        self.add((URIRef(f"element_{Element.Uid}"), self.moosas.Offset, Literal(Element.offset)))
        element_uri = URIRef(f"element_{Element.Uid}")
        self.add((element_uri, self.moosas.U_Value, Literal(Element.U_Value)))
        self.add((URIRef(f"element_{Element.Uid}"), self.pgd.hasSurfaceType, self.moosas.term(typeName)))
        self.add((URIRef(f"element_{Element.Uid}"), self.moosas.hasLevel, URIRef(f"Level_{Element.level}")))
        self.add((URIRef(f"element_{Element.Uid}"), self.pgd.hasArea_m2, Literal(Element.area)))
        self.add((URIRef(f"element_{Element.Uid}"), self.pgd.hasNormalVectorX_m, Literal(Element.normal[0])))
        self.add((URIRef(f"element_{Element.Uid}"), self.pgd.hasNormalVectorY_m, Literal(Element.normal[1])))
        self.add((URIRef(f"element_{Element.Uid}"), self.pgd.hasNormalVectorZ_m, Literal(Element.normal[2])))
        condition = "Outdoors" if Element.isOuter else "Indoors"
        self.add((URIRef(f"element_{Element.Uid}"), self.pgd.hasOutsideBoundaryCondition, Literal(condition)))
        for fid in mixItemListToList(Element.faceId):
            self.add((URIRef(f"element_{Element.Uid}"), self.moosas.hasFace, URIRef(fid)))
        for gid in mixItemListToList(Element.glazingId):
            self.add((URIRef(f"element_{Element.Uid}"), self.bot.hasSubElement, URIRef(f"element_{gid}")))
        for key in Element.neighbor:
            for neiElement in Element.neighbor[key]:
                if mask:
                    if neiElement in mask:
                        self.add((URIRef(f"element_{Element.Uid}"), self.moosas.hasNeighborElement,
                                  URIRef(f"element_{neiElement}")))

        if len(Element.space) > 1 and Element.category == 2:
            self.add((URIRef(f"element_{Element.Uid}"), self.pgd.hasAirFlow, URIRef(f"Space_{Element.space[0]}")))
            self.add(
                (URIRef(f"element_{Element.Uid}"), self.pgd.hasAirFlow, URIRef(f"Space_{Element.space[1]}")))
        # Keep legacy element predicates while making the editable element
        # settings round-trip as a single extensible RDF representation.
        for key, value in getattr(Element, 'settings', {}).items():
            if value is None:
                continue
            self.add((element_uri, self.moosas.hasSetting, Literal(key)))
            self.add((element_uri, self.moosas[key], _setting_literal(value)))
        for key in getattr(Element, 'explicit_settings', set()):
            self.add((element_uri, self.moosas.explicitSetting, Literal(key)))
        if typeName in ("Glazing", "Skylight"):
            shgc = getattr(Element, "SHGC", None)
            if shgc is not None:
                self.add((URIRef(f"element_{Element.Uid}"), self.moosas.SHGC, Literal(shgc)))
            operable = getattr(Element, "operable", None)
            if operable is not None:
                self.add((URIRef(f"element_{Element.Uid}"), self.moosas.operable, Literal(operable)))

        # ifc related objects
        if ExportIFC:
            gbID = generate_code(22)
            self.add((URIRef(f"ifcElement_{Element.Uid}"), self.ifc.GlobalID, Literal(gbID)))
            self.add((URIRef(f"ifcElement_{Element.Uid}"), self.ifc.refElement, URIRef(f"element_{Element.Uid}")))
            if typeName == 'Wall':
                self.add((URIRef(f"ifcElement_{Element.Uid}"), self.rdf.type, self.ifc.IfcWall))
            if typeName == 'Face':
                self.add((URIRef(f"ifcElement_{Element.Uid}"), self.rdf.type, self.ifc.IfcSlab))
            if typeName == 'Glazing' or typeName == 'Skylight':
                self.add((URIRef(f"ifcElement_{Element.Uid}"), self.rdf.type, self.ifc.IfcWindow))
            if typeName == 'AirWall' or typeName == 'AirSkylight':
                self.add((URIRef(f"ifcElement_{Element.Uid}"), self.rdf.type, self.ifc.IfcVirtualElement))

    def encodeStorey(self, model: MoosasModel):
        """
        Encode building storeys and their associated spaces into the RDF graph.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the method. Holds the RDF graph and namespaces.
        model : MoosasModel
            The model containing level and space information to be encoded. Must have `levelList` 
            and `spaceList` attributes, where `levelList` contains elevation levels and `spaceList` 
            contains space objects with 'level' and 'id' properties.
        
        Returns
        -------
        None
            This function modifies the RDF graph in place and does not return any value.
        """
        for bld_level in model.levelList:
            bld = self.getSubject(self.rdf.type, self.bot.Building)
            self.add((URIRef(f"Level_{bld_level}"), self.rdf.type, self.bot.Storey))
            self.add((URIRef(str(bld)), self.bot.hasStorey, URIRef(f"Level_{bld_level}")))
            self.add((URIRef(f"Level_{bld_level}"), self.moosas.altitute, Literal(bld_level)))
            spaces = np.array(model.spaceList)[searchBy('level', bld_level, model.spaceList)]
            for space in spaces:
                self.add((URIRef(f"Level_{bld_level}"), self.bot.hasSpace, URIRef(f"Space_{space.id}")))

    def encode2LSB(self, spaceId: str, element: MoosasElement):
        """
        Encode a building element into a second-level space boundary representation using RDF triples.
        
        Parameters
        ----------
        spaceId : str
            Identifier for the space, used to construct URIs and determine spatial relationships.
            Special value 'outer' indicates an external spatial element.
        element : MoosasElement
            The building element to encode, containing properties such as Uid, category, level,
            normal vector, space membership, and glazing elements.
        
        Returns
        -------
        None
            This function does not return a value. It modifies the internal state by adding RDF triples
            representing the IfcRelSpaceBoundary2ndLevel relationship.
        """
        gbID = generate_code(22)
        self.add((URIRef(f"{spaceId}_{element.Uid}"), self.rdf.type, self.ifc.IfcRelSpaceBoundary2ndLevel))
        self.add((URIRef(f"{spaceId}_{element.Uid}"), self.ifc.GlobalID, Literal(gbID)))
        self.add((URIRef(f"{spaceId}_{element.Uid}"), self.ifc.Description, Literal("2a")))
        self.add((URIRef(f"{spaceId}_{element.Uid}"), self.ifc.RelatedBuildingElement,
                  URIRef(f"ifcElement_{element.Uid}")))
        if element.category == 2:
            self.add((URIRef(f"{spaceId}_{element.Uid}"), self.ifc.PhysicalOrVirtualBoundary,
                      URIRef(str('VIRTUAL'))))
        else:
            self.add((URIRef(f"{spaceId}_{element.Uid}"), self.ifc.PhysicalOrVirtualBoundary,
                      URIRef(str('PHYSICAL'))))
        if spaceId == 'outer':
            self.add((URIRef(f"{spaceId}_{element.Uid}"), self.ifc.RelatingSpace, self.ifc.IfcExternalSpatialElement))
            if element.parent.levelList.index(element.level) == 0 and Vector.parallel(element.normal, [0, 0, 1]):
                self.add((URIRef(f"{spaceId}_{element.Uid}"), self.ifc.InternalOrExternalBoundary,
                          Literal("EXTERNAL_EARTH")))
            else:
                self.add((URIRef(f"{spaceId}_{element.Uid}"), self.ifc.InternalOrExternalBoundary, Literal("EXTERNAL")))
        else:
            self.add((URIRef(f"{spaceId}_{element.Uid}"), self.ifc.RelatingSpace, URIRef(f"ifcSpace_{spaceId}")))
            self.add((URIRef(f"{spaceId}_{element.Uid}"), self.ifc.InternalOrExternalBoundary, Literal("INTERNAL")))
            if element.isOuter:
                self.encode2LSB("outer", element)
                self.add((URIRef(f"{spaceId}_{element.Uid}"), self.ifc.CorrespondingBoundary,
                          URIRef(f"outer_{element.Uid}")))
                self.add((URIRef(f"outer_{element.Uid}"), self.ifc.CorrespondingBoundary,
                          URIRef(f"{spaceId}_{element.Uid}")))
            else:
                otherSpace = element.space[1] if element.space.index(spaceId) == 0 else element.space[0]
                self.add((URIRef(f"{spaceId}_{element.Uid}"), self.ifc.CorrespondingBoundary,
                          URIRef(f"{otherSpace}_{element.Uid}")))

        for glsEle in element.glazingElement:
            self.encode2LSB("outer", glsEle)
            self.add((URIRef(f"{spaceId}_{glsEle.Uid}"), self.ifc.ParentBoundary, URIRef(f"{spaceId}_{element.Uid}")))

    def encodeSpace(self, space: MoosasSpace, ExportIFC=False):
        """
        Encode a MoosasSpace object into RDF triples within the graph, optionally exporting to IFC format.
        
        Parameters
        ----------
        space : MoosasSpace
            The space object to be encoded, containing properties such as id, area, height, ceiling, floor, edge, and voids.
        ExportIFC : bool, optional
            If True, exports the space and associated elements to IFC-compatible RDF triples. Default is False.
        
        Returns
        -------
        None
            This function does not return any value. It modifies the graph state by adding RDF triples.
        """
        self.add((URIRef(f"Space_{space.id}"), self.rdf.type, self.bot.Space))
        self.add((URIRef(f"Space_{space.id}"), self.rdfs.comment, Literal(space.description)))
        self.add((URIRef(f"Space_{space.id}"), self.moosas.Uid, Literal(space.id)))
        # Semantic type must be restored before ``is_void`` validates inclined roofs.
        self.add((URIRef(f"Space_{space.id}"), self.moosas.spaceType,
                  Literal("void" if self._space_is_void(space) else getattr(space, "space_type", "room"))))
        self.add((URIRef(f"Space_{space.id}"), self.moosas.isVoid,
                  Literal(self._space_is_void(space))))
        self.add((URIRef(f"Space_{space.id}"), self.moosas.conditioned,
                  Literal(space.conditioned)))
        self.add((URIRef(f"Space_{space.id}"), self.moosas.boundaryWKT,
              Literal(str(space.edge.force_2d()), datatype=self.geo.wktLiteral)))

        def _add_interface(interface_name: str, linked_element_uri: str, surface_type_uri, opaque_surface_uri=None):
            interface_uri = URIRef(interface_name)
            self.add((interface_uri, self.rdf.type, self.bot.Interface))
            self.add((interface_uri, self.pgd.surfaceType, URIRef(str(surface_type_uri))))
            if opaque_surface_uri is not None:
                self.add((interface_uri, self.pgd.hasSurfaceType, URIRef(str(opaque_surface_uri))))
            self.add((interface_uri, self.bot.interfaceOf, URIRef(f"Space_{space.id}")))
            self.add((interface_uri, self.bot.interfaceOf, URIRef(linked_element_uri)))

        # storage space settings
        for key in space.settings:
            value = space.settings[key]
            if value is None:
                continue
            if hasattr(value, "applyToIDF"):
                continue
            self.add((URIRef(f"Space_{space.id}"), self.moosas.hasSetting, Literal(key)))

            self.add((URIRef(f"Space_{space.id}"), self.moosas[str(key)], _setting_literal(value)))
        for key in getattr(space, 'explicit_settings', []):
            self.add((URIRef(f"Space_{space.id}"), self.moosas.explicitSetting, Literal(key)))

        self.add((URIRef(f"Space_{space.id}"), self.pgd.hasFloorArea_m2, Literal(space.area)))
        self.add((URIRef(f"Space_{space.id}"), self.pgd.hasVolume_m3, Literal(space.area * space.height)))
        self.add((URIRef(f"Space_{space.id}"), self.pgd.hasNorthDirection_deg, Literal(0e+00)))
        ifcElement = []
        if space.ceiling:
            # self.add((URIRef(f"ceil_{space.ceiling.Uid}"), self.rdf.type, self.moosas.TopoElement))
            # self.add((URIRef(f"Space_{space.id}"), self.bot.adjacentElement, URIRef(f"ceil_{space.ceiling.Uid}")))
            # self.add((URIRef(f"ceil_{space.ceiling.Uid}"), self.pgd.hasSurfaceType, self.moosas.Ceiling))
            for faces in mixItemListToList(space.ceiling.face):
                # self.add((URIRef(f"ceil_{space.ceiling.Uid}"), self.bot.hasSubElement, URIRef(f"element_{faces.Uid}")))
                self.add((URIRef(f"Space_{space.id}"), self.bot.adjacentElement, URIRef(f"element_{faces.Uid}")))
                ceiling_surface = self.pgd.Roof if faces.isOuter else self.pgd.Ceiling
                _add_interface(f"{space.id}_{faces.Uid}", f"element_{faces.Uid}", ceiling_surface, self.moosas.Ceiling)
                ifcElement.append(faces)
                for gls in mixItemListToList(faces.glazingElement):
                    self.add((URIRef(f"Space_{space.id}"), self.bot.adjacentElement, URIRef(f"element_{gls.Uid}")))
                    opening_surface = self.pgd.AirWindow if getattr(gls, "category", 0) == 2 else self.pgd.OperableWindow
                    _add_interface(f"{space.id}_{gls.Uid}", f"element_{gls.Uid}", opening_surface)

        if space.floor:
            # self.add((URIRef(f"floor_{space.floor.Uid}"), self.rdf.type, self.moosas.TopoElement))
            # self.add((URIRef(f"Space_{space.id}"), self.bot.adjacentElement, URIRef(f"floor_{space.floor.Uid}")))
            # self.add((URIRef(f"floor_{space.floor.Uid}"), self.pgd.hasSurfaceType, self.moosas.Floor))
            for faces in mixItemListToList(space.floor.face):
                # self.add((URIRef(f"floor_{space.floor.Uid}"), self.bot.hasSubElement, URIRef(f"element_{faces.Uid}")))
                self.add((URIRef(f"Space_{space.id}"), self.bot.adjacentElement, URIRef(f"element_{faces.Uid}")))
                if faces.isOuter:
                    floor_surface = self.pgd.SlabOnGrade if float(faces.level) <= min(space.parent.levelList) else self.pgd.UndergroundSlab
                else:
                    floor_surface = self.pgd.InteriorFloor
                _add_interface(f"{space.id}_{faces.Uid}", f"element_{faces.Uid}", floor_surface, self.moosas.Floor)
                ifcElement.append(faces)
                for gls in mixItemListToList(faces.glazingElement):
                    self.add((URIRef(f"Space_{space.id}"), self.bot.adjacentElement, URIRef(f"element_{gls.Uid}")))
                    opening_surface = self.pgd.AirWindow if getattr(gls, "category", 0) == 2 else self.pgd.OperableWindow
                    _add_interface(f"{space.id}_{gls.Uid}", f"element_{gls.Uid}", opening_surface)

        if space.edge:
            # self.add((URIRef(f"edge_{space.edge.Uid}"), self.rdf.type, self.moosas.TopoElement))
            # self.add((URIRef(f"Space_{space.id}"), self.bot.adjacentElement, URIRef(f"edge_{space.edge.Uid}")))
            # self.add((URIRef(f"edge_{space.edge.Uid}"), self.pgd.hasSurfaceType, self.moosas.Edge))
            loop = []
            for lp,wall in enumerate(mixItemListToList(space.edge.wall)):
                # self.add((URIRef(f"edge_{space.edge.Uid}"), self.bot.hasSubElement, URIRef(f"element_{wall.Uid}")))
                self.add((URIRef(f"Space_{space.id}"), self.bot.adjacentElement, URIRef(f"element_{wall.Uid}")))
                self.add((URIRef(f"{space.id}_{wall.Uid}"), self.moosas.subElementOrder, Literal(lp)))
                edge_surface = self.pgd.ExteriorWall if wall.isOuter else self.pgd.InteriorWall
                _add_interface(f"{space.id}_{wall.Uid}", f"element_{wall.Uid}", edge_surface, self.moosas.Edge)
                loop.append(wall.Uid)
                ifcElement.append(wall)
                for gls in mixItemListToList(wall.glazingElement):
                    self.add((URIRef(f"Space_{space.id}"), self.bot.adjacentElement, URIRef(f"element_{gls.Uid}")))
                    opening_surface = self.pgd.AirWindow if getattr(gls, "category", 0) == 2 else self.pgd.OperableWindow
                    _add_interface(f"{space.id}_{gls.Uid}", f"element_{gls.Uid}", opening_surface)
            # self.add((URIRef(f"edge_{space.edge.Uid}"), self.moosas.subElementOrder, Literal(','.join(loop))))
        for void in space.void:
            self.add((URIRef(f"Space_{space.id}"), self.bot.containsZone, URIRef(f"Space_{void.id}")))

        if ExportIFC:
            self.add((URIRef(f"ifcSpace_{space.id}"), self.rdf.type, self.ifc.IfcSpace))
            self.add((URIRef(f"ifcSpace_{space.id}"), self.ifc.GlobalID, Literal(generate_code(22))))
            self.add((URIRef(f"ifcSpace_{space.id}"), self.moosas.refSpace, (URIRef(f"Space_{space.id}"))))
            for element in ifcElement:
                self.encode2LSB(space.id, element)

    def decodeGeo(self, geoUri, model: MoosasModel = None) -> MoosasGeometry:
        """
        Decode a geographic URI into a MoosasGeometry object.
        
        Parameters
        ----------
        geoUri : str or rdflib.term.URIRef
            The geographic URI to decode. If a string is provided, it will be converted to a URIRef.
        model : MoosasModel, optional
            An optional model used to look up the face by face ID. If provided and a matching geometry is found, it will be returned directly.
        
        Returns
        -------
        MoosasGeometry
            A MoosasGeometry object representing the decoded geometry, including face, face ID, category, and any holes.
        """
        if isinstance(geoUri, str):
            geoUri = URIRef(str(geoUri))
        faceId = _first_or_none(self.getObject(geoUri, self.moosas.faceId))
        if model:
            geo = model.findFace(str(faceId))
            if len(geo) > 0:
                return geo[0]
        cat = int(float(_first_or_none(self.getObject(geoUri, self.moosas.Category))))
        face = URIRef(str(_first_or_none(self.getObject(geoUri, self.geo.hasGeometry))))
        face = shapely.from_wkt(str(_first_or_none(self.getObject(face, self.geo.asWKT))))
        hole_values = self.getObject(geoUri, self.moosas.hasHole)
        if hole_values is not None:
            holes = []
            for hole in mixItemListToList(hole_values):
                hole = _first_or_none(self.getObject(URIRef(str(hole)), self.geo.asWKT))
                if hole:
                    holes.append(shapely.from_wkt(str(hole)))

        else:
            holes = []
        return MoosasGeometry(face=face, faceId=faceId, category=cat, holes=holes, errors="ignore")

    def decodeElement(self, elementUri, model: MoosasModel = None) -> MoosasElement | None:
        """
        Decode an element from its URI by retrieving and interpreting semantic information.
        
        Parameters
        ----------
        elementUri : str or rdflib.term.URIRef
            The URI reference of the element to decode. If a string is provided, it will be converted to a URIRef.
        model : MoosasModel, optional
            The model instance containing element lists (e.g., faceList, wallList). Used to search for existing elements. 
            If not provided, a new element will be constructed based on retrieved properties.
        
        Returns
        -------
        MoosasElement or None
            The decoded MoosasElement instance if found or successfully created; otherwise, None.
        """
        if isinstance(elementUri, str):
            elementUri = URIRef(str(elementUri))

        def apply_element_settings(element):
            """Read new generic element settings while accepting legacy RDF."""
            settings = {}
            for key_literal in _as_rdf_values(self.getObject(elementUri, self.moosas.hasSetting)):
                key = str(_literal_to_python(key_literal))
                value = _first_or_none(self.getObject(elementUri, self.moosas[key]))
                if value is not None:
                    settings[key] = _literal_to_python(value)
            element.settings.update(settings)
            element.explicit_settings = {
                str(_literal_to_python(v))
                for v in _as_rdf_values(self.getObject(elementUri, self.moosas.explicitSetting))
            }
            if 'u_value' in settings:
                element.U_Value = float(settings['u_value'])
            if isinstance(element, (MoosasGlazing, MoosasSkylight)):
                if 'shgc' in settings:
                    element.SHGC = float(settings['shgc'])
                if 'operable' in settings:
                    element.operable = float(settings['operable'])

        Uid = str(self.getObject(elementUri, self.moosas.Uid))
        surfaceTypeRaw = _first_or_none(self.getObject(elementUri, self.pgd.hasSurfaceType))
        surfaceType = URIRef(str(surfaceTypeRaw)) if surfaceTypeRaw is not None else None
        if surfaceType is None:
            return None

        cache=getattr(self,'_decode_element_cache',None)
        cache_key=(id(model),str(elementUri))
        if cache is not None:
            element=[cache[cache_key]] if cache_key in cache else []
        elif surfaceType == self.moosas.Face:
            element = searchBy('Uid', Uid, model.faceList, earlyEnd=True, asObject=True)
        elif surfaceType == self.moosas.Wall or surfaceType == self.moosas.AirWall:
            element = searchBy('Uid', Uid, model.wallList, earlyEnd=True, asObject=True)
        elif surfaceType == self.moosas.Glazing:
            element = searchBy('Uid', Uid, model.glazingList, earlyEnd=True, asObject=True)
        elif surfaceType == self.moosas.Skylight or surfaceType == self.moosas.AirSkylight:
            element = searchBy('Uid', Uid, model.skylightList, earlyEnd=True, asObject=True)
        else:
            return None
        if len(element) > 0:
            element = element[0]
            u_value = _first_or_none(self.getObject(elementUri, self.moosas.U_Value))
            if u_value is not None:
                try:
                    element.U_Value = float(_literal_to_python(u_value))
                    element.explicit_settings.add('u_value')
                except Exception:
                    pass
            if isinstance(element, (MoosasGlazing, MoosasSkylight)):
                shgc = _first_or_none(self.getObject(elementUri, self.moosas.SHGC))
                if shgc is not None:
                    try:
                        element.SHGC = float(_literal_to_python(shgc))
                        element.explicit_settings.add('shgc')
                    except Exception:
                        pass
                operable = _first_or_none(self.getObject(elementUri, self.moosas.operable))
                if operable is not None:
                    try:
                        element.operable = float(_literal_to_python(operable))
                    except Exception:
                        pass
            apply_element_settings(element)
            return element

        offset = float(self.getObject(elementUri, self.moosas.Offset))
        level = self.getObject(elementUri, self.moosas.hasLevel)
        level = float(self.getObject(URIRef(str(level)), self.moosas.altitute))
        geoId = mixItemListToList(self.getObject(elementUri, self.moosas.hasFace))
        geoId = mixItemListToObject([str(self.getObject(URIRef(gi), self.moosas.faceId)) for gi in geoId])
        if surfaceType == self.moosas.Face:
            element = MoosasFace(model, geoId, level=level, uid=Uid, offset=offset)
        elif surfaceType == self.moosas.Wall or surfaceType == self.moosas.AirWall:
            element = MoosasWall(model, geoId, level=level, uid=Uid, offset=offset)
        elif surfaceType == self.moosas.Glazing:
            element = MoosasGlazing(model, geoId, level=level, uid=Uid, offset=offset)
        elif surfaceType == self.moosas.Skylight or surfaceType == self.moosas.AirSkylight:
            element = MoosasSkylight(model, geoId, level=level, uid=Uid, offset=offset)
        else:
            element = None
        if element is None:
            return None
        u_value = _first_or_none(self.getObject(elementUri, self.moosas.U_Value))
        if u_value is not None:
            try:
                element.U_Value = float(_literal_to_python(u_value))
                element.explicit_settings.add('u_value')
            except Exception:
                pass
        if isinstance(element, (MoosasGlazing, MoosasSkylight)):
            shgc = _first_or_none(self.getObject(elementUri, self.moosas.SHGC))
            if shgc is not None:
                try:
                    element.SHGC = float(_literal_to_python(shgc))
                    element.explicit_settings.add('shgc')
                except Exception:
                    pass
            operable = _first_or_none(self.getObject(elementUri, self.moosas.operable))
            if operable is not None:
                try:
                    element.operable = float(_literal_to_python(operable))
                except Exception:
                    pass
        apply_element_settings(element)
        if cache is not None:cache[cache_key]=element
        return element

    def isClass(self, _from: str, _class: URIRef) -> bool:
        """Check if the given subject is an instance of the specified class.
        
            Parameters
            ----------
            _from : str
                The subject URI as a string.
            _class : rdflib.term.URIRef
                The class URI to check against, represented as a URIRef.
        
            Returns
            -------
            bool
                True if the subject has the specified class as its type, False otherwise.
        """
        return self.getObject(_from, self.rdf.type) == _class

    def getObject(self, _from, _property):
        """
        Get a list of objects associated with a given subject and property.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the `objects` method and `mixItemListToObject` function.
        _from : hashable
            The subject or source entity from which to retrieve associated objects.
        _property : hashable
            The property or predicate used to filter the relationships.
        
        Returns
        -------
        list
            A list of objects obtained by collecting unique values from the `objects` generator and converting them using `mixItemListToObject`.
        """
        objects = set()
        for o in self.objects(_from, _property):
            objects.add(o)
        if len(objects) == 0:
            return None
        elif len(objects) == 1:
            return list(objects)[0]
        else:
            return list(objects)

    def getSubject(self, _property, _to):
        """
        Get a list of subjects for a given property and object, returned as a mixed item list converted to objects.
        
        Parameters
        ----------
        _property : str or rdflib.term.URIRef
            The property (predicate) to match in the RDF triples.
        _to : str or rdflib.term.Identifier
            The object value to match in the RDF triples.
        
        Returns
        -------
        list
            A list of subject objects obtained from matching triples, with mixed items converted into objects.
        """
        objects = set()
        for o in self.subjects(_property, _to):
            objects.add(o)

        return mixItemListToObject(list(objects))

    def getRelate(self, node) -> list:
        """
        Get all nodes related to the given node through outgoing or incoming triples.
        
        Parameters
        ----------
        node : hashable
            The node for which related nodes are to be retrieved. Can be any hashable type representing a subject or object in the triples.
        
        Returns
        -------
        list
            A list of nodes that are related to the input node, either as objects in subject-predicate-node triples or as subjects in node-predicate-object triples. Duplicates are removed using a set.
        """
        related = set()
        for s, p, o in self.triples((node, None, None)):
            related.add(o)
        for s, p, o in self.triples((None, None, node)):
            related.add(s)
        return list(related)

    def entities(self, check=False):
        uriref_subjects = set()  # 存储所有URIRef类型的主语（去重）
        uriref_objects = set()  # 存储所有URIRef类型的宾语（去重）
        for s, p, o in self:
            # 判断主语是否为 URIRef 类型
            if isinstance(s, URIRef):
                uriref_subjects.add(s)  # 去重存储

            # 判断宾语是否为 URIRef 类型
            if isinstance(o, URIRef):
                uriref_objects.add(o)  # 去重存储
        allEntities = {}
        for entity in list(uriref_subjects) + list(uriref_objects):
            entJson = self.get_entity(entity, check)
            allEntities[decodeURI(entity)] = entJson
        return list(allEntities.values())

    def get_entity(self, entityURI, check=False):
        if not isinstance(entityURI, URIRef):
            entity = encodeURI(entityURI)
        else:
            entity = entityURI

        entJson = {"uri": decodeURI(entity)}
        label = self.getObject(entity, RDFS.label)
        if len(label) > 0:
            entJson['label'] = str(label[0])
        elif check:
            print("Skipping entity " + str(entity) + " empty label")

        description = self.getObject(entity, RDFS.comment)
        if len(description) > 0:
            entJson['comment'] = str(description[0])
        elif check:
            print("Skipping entity " + str(entity) + " empty comment")

        objType = self.getObject(entity, RDF.type)
        if len(objType) > 0:
            entJson['type'] = decodeURI(objType[0])[len(self.idfNameSpace):]
        elif check:
            print("Skipping entity " + str(entity) + " empty type")
        return entJson

    def encode_entity(self, name: str, entityType: URIRef, description: str, label: str = None):
        if label is None:
            label = name
        self.add((encodeURI(name), RDFS.label, Literal(label)))
        self.add((encodeURI(name), RDF.type, entityType))
        self.add((encodeURI(name), RDFS.comment, Literal(description)))
        return encodeURI(name)


def _import_schedule_nodes(rdfGraph: MoosasRDF, model: MoosasModel):
    model.schedule = {}
    schedule_nodes = set()
    for cls in (rdfGraph.pgd.DailySchedule, rdfGraph.pgd.WeeklySchedule):
        nodes = rdfGraph.getSubject(rdfGraph.rdf.type, cls)
        for node in mixItemListToList(nodes) if nodes is not None else []:
            schedule_nodes.add(URIRef(str(node)))

    for scheduleUri in sorted(schedule_nodes, key=str):
        schedule_name = rdfGraph.getObject(scheduleUri, rdfGraph.pgd.scheduleName)
        if schedule_name is None:
            schedule_name = decodeURI(scheduleUri)
        schedule_name = str(schedule_name)
        schedule_type = _first_or_none(rdfGraph.getObject(scheduleUri, rdfGraph.rdf.type))
        schedule_type = str(schedule_type).rsplit("#", 1)[-1].replace("Schedule", "")
        if schedule_type.lower() == "daily":
            values = rdfGraph.getObject(scheduleUri, rdfGraph.pgd.hourlyValuesJson)
            values = _literal_to_python(values)
            if isinstance(values, str):
                values = json.loads(values)
            model.schedule[schedule_name] = {
                "type": "Daily",
                "value": [float(v) if _is_numeric_text(v) else v for v in list(values or [])],
            }
        elif schedule_type.lower() == "weekly":
            day_props = [
                rdfGraph.pgd.mondaySchedule,
                rdfGraph.pgd.tuesdaySchedule,
                rdfGraph.pgd.wednesdaySchedule,
                rdfGraph.pgd.thursdaySchedule,
                rdfGraph.pgd.fridaySchedule,
                rdfGraph.pgd.saturdaySchedule,
                rdfGraph.pgd.sundaySchedule,
            ]
            values = []
            for prop in day_props:
                day_value = _first_or_none(rdfGraph.getObject(scheduleUri, prop))
                values.append(_decode_space_setting_value(day_value) if day_value is not None else "")
            model.schedule[schedule_name] = {
                "type": "Weekly",
                "value": values,
            }


def _decode_space_settings(rdfGraph: MoosasRDF, spaceUri, spc: MoosasSpace):
    spcSettings = mixItemListToList(rdfGraph.getObject(spaceUri, rdfGraph.moosas.hasSetting))
    for key in spcSettings:
        if str(key) == 'zone_inflitration':
            raise ValueError('zone_inflitration is unsupported; use zone_infiltration')
        raw_value = _first_or_none(rdfGraph.getObject(spaceUri, rdfGraph.moosas[str(key)]))
        canonical = raw_value is not None
        if not canonical:
            raw_value = _first_or_none(rdfGraph.getObject(spaceUri, Literal(key)))
        spc.settings[str(key)] = _literal_to_python(raw_value) if canonical else _decode_space_setting_value(raw_value)
    spc.explicit_settings = [str(k) for k in rdfGraph.objects(spaceUri, rdfGraph.moosas.explicitSetting)]
    validate_template_snapshot(spc.settings)


# Editable construction metadata.  Volumetric heat capacity is stored in SI
# units and is consumed directly by the 5R1C adapter.
_WALL_DEFAULTS = {
    'u_value': 0.5,
    'volumetric_heat_capacity': 2116000.0,
    'thickness': 0.2,
    'reflection': 0.5,
}
_GLAZING_DEFAULTS = {
    'u_value': 2.4,
    'shgc': 0.6,
    'operable': 0.5,
    'reflection': 0.1,
    'transparency': 0.7,
}


def _element_space_owners(model):
    """Return deterministic owner spaces, including a space's openings."""
    owners = {}
    for space in list(model.spaceList) + list(model.voidList):
        for element in space.getAllFaces(to_dict=False):
            owners.setdefault(id(element), []).append(space)
    return {key: sorted(value, key=lambda space: str(space.id))
            for key, value in owners.items()}


def _numeric_or_default(value, default):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return float(default)
    return value if np.isfinite(value) else float(default)


def _space_parameter(owners, setting, default):
    """Use the first non-null owner value for a shared element deterministically."""
    for space in owners:
        value = getattr(space, 'settings', {}).get(setting)
        if value is not None:
            return _numeric_or_default(value, default)
    return float(default)


def _rdf_element_setting_value(rdfGraph, element, key):
    """Return the RDF value, preferring editable generic settings over legacy predicates."""
    # Relative ``element_*`` URIs acquire a file:// base when parsed. Resolve by
    # stable Moosas Uid rather than reconstructing a relative URI.
    uris = list(rdfGraph.subjects(rdfGraph.moosas.Uid, Literal(str(element.Uid))))
    if not uris:
        uris = [URIRef(f"element_{element.Uid}")]
    predicates = [rdfGraph.moosas[key], Literal(key)]
    if key == 'u_value':
        predicates.append(rdfGraph.moosas.U_Value)
    elif key == 'shgc':
        predicates.append(rdfGraph.moosas.SHGC)
    elif key == 'operable':
        predicates.append(rdfGraph.moosas.operable)
    for uri in uris:
        for predicate in predicates:
            value = next(iter(rdfGraph.objects(uri, predicate)), None)
            if value is not None:
                return _literal_to_python(value)
    return None


def _set_element_setting(element, key, value):
    if key == 'u_value':
        element.U_Value = _numeric_or_default(value, _WALL_DEFAULTS['u_value'])
    elif key == 'shgc':
        element.SHGC = _numeric_or_default(value, _GLAZING_DEFAULTS['shgc'])
    elif key == 'operable':
        element.operable = _numeric_or_default(value, _GLAZING_DEFAULTS['operable'])
    else:
        element.settings[key] = _numeric_or_default(value, 0.0)


def _apply_element_parameter_defaults(model, rdfGraph=None):
    """Fill only missing opaque/glazing construction settings.

    With ``rdfGraph``, fields supplied by RDF are preserved. This prevents
    constructor defaults from masking the space-level U/SHGC fallback for old RDF.
    """
    owners = _element_space_owners(model)
    opaque = {}
    for space in list(model.spaceList) + list(model.voidList):
        groups = space.getAllFaces(to_dict=True)
        for role in ('MoosasWall', 'MoosasFloor', 'MoosasCeiling', 'InternalMass'):
            for element in groups.get(role, ()):
                opaque[id(element)] = element
    groups = (
        (list(opaque.values()), _WALL_DEFAULTS, {'u_value': 'zone_wallU'}),
        (list(model.glazingList) + list(model.skylightList), _GLAZING_DEFAULTS,
         {'u_value': 'zone_winU', 'shgc': 'zone_win_SHGC'}),
    )
    legacy_capacity_elements = []
    for elements, defaults, space_fields in groups:
        for element in elements:
            settings = element.settings
            element_owners = owners.get(id(element), ())
            legacy_capacity = settings.pop('heat_storage_coefficient', None)
            for key, fallback in defaults.items():
                supplied = key in settings and settings[key] is not None
                if rdfGraph is not None:
                    rdf_value = _rdf_element_setting_value(rdfGraph, element, key)
                    if rdf_value is not None:
                        _set_element_setting(element, key, rdf_value)
                        continue
                    supplied = False
                if supplied:
                    continue
                value = (_space_parameter(element_owners, space_fields[key], fallback)
                         if key in space_fields else float(fallback))
                _set_element_setting(element, key, value)
            if rdfGraph is not None and legacy_capacity is None:
                legacy_capacity = _rdf_element_setting_value(
                    rdfGraph, element, 'heat_storage_coefficient')
            if legacy_capacity is not None:
                legacy_capacity_elements.append(str(element.Uid))
    if legacy_capacity_elements:
        warnings.warn(
            "Legacy heat_storage_coefficient is not a volumetric heat capacity; "
            "using volumetric_heat_capacity defaults for " + ", ".join(legacy_capacity_elements),
            UserWarning,
            stacklevel=2,
        )


def _decode_space_type(rdfGraph: MoosasRDF, spaceUri) -> str:
    """Read an optional semantic type; old RDF files default to a normal room."""
    raw_value = _first_or_none(rdfGraph.getObject(spaceUri, rdfGraph.moosas.spaceType))
    value = _literal_to_python(raw_value)
    return str(value).strip().lower() if value is not None else "room"


def _decode_conditioned(rdfGraph: MoosasRDF, spaceUri) -> bool:
    raw_value = _first_or_none(rdfGraph.getObject(spaceUri, rdfGraph.moosas.conditioned))
    value = _literal_to_python(raw_value)
    return True if value is None else bool(value)


def writeRDF(model: MoosasModel, out_path: str, fileFormat="turtle", dumpUseless=True, ExportIFC=False):
    """
    Serialize a MoosasModel to an RDF file in the specified format.
    
    Parameters
    ----------
    model : MoosasModel
        The MoosasModel instance to be serialized into RDF.
    out_path : str
        The file path where the RDF output will be written.
    fileFormat : str, optional
        The serialization format for the RDF output (e.g., 'turtle', 'xml'). Default is "turtle".
    dumpUseless : bool, optional
        If True, includes unnecessary or auxiliary information in the output. Default is True.
    ExportIFC : bool, optional
        If True, exports IFC-related data in the RDF output. Default is False.
    
    Returns
    -------
    MoosasRDF
        The generated MoosasRDF object that was serialized to the file.
    """
    g = MoosasRDF(model, dumpUseless, ExportIFC)
    output_format='nt' if fileFormat=='turtle' and len(g)>100000 else fileFormat
    if output_format == 'nt':
        from pathlib import Path
        from urllib.parse import urlsplit, urljoin
        base = Path(out_path).resolve().as_uri()
        relative = {node: URIRef(urljoin(base, str(node)))
                    for node in set(g.all_nodes()) | set(g.predicates())
                    if isinstance(node, URIRef) and not urlsplit(str(node)).scheme}
        for node in relative:
            triples = (set(g.triples((node, None, None))) |
                       set(g.triples((None, node, None))) |
                       set(g.triples((None, None, node))))
            for triple in triples:
                g.remove(triple)
                g.add(tuple(relative.get(term, term) for term in triple))
    g.serialize(out_path,format=output_format,encoding='utf-8')
    return g


def loadRDF(input_path: str, fileFormat="turtle") -> MoosasModel:
    """
    Load RDF data from a file and construct a MoosasModel instance.
    
    Parameters
    ----------
    input_path : str
        Path to the input RDF file.
    fileFormat : str, optional
        Format of the RDF file (default is "turtle").
    
    Returns
    -------
    MoosasModel
        A constructed MoosasModel instance populated with data from the RDF file.
    """
    rdfGraph: MoosasRDF = MoosasRDF.load(input_path, fileFormat=fileFormat)
    rdfGraph._decode_element_cache={}
    model = MoosasModel()
    from ..building_templates import configure_model_resources

    configure_model_resources(model)
    _import_schedule_nodes(rdfGraph, model)

    print(f'\rLOADING: searching Objects', end='')
    geoList = rdfGraph.getSubject(rdfGraph.rdf.type, rdfGraph.moosas.Geometry)
    geoList = mixItemListToList(geoList)
    levelList = rdfGraph.getSubject(rdfGraph.rdf.type, rdfGraph.bot.Storey)
    levelList = mixItemListToList(levelList)
    moFaceList = rdfGraph.getSubject(rdfGraph.pgd.hasSurfaceType, rdfGraph.moosas.Face)
    moFaceList = mixItemListToList(moFaceList)
    moWallList = rdfGraph.getSubject(rdfGraph.pgd.hasSurfaceType, rdfGraph.moosas.Wall)
    moWallList = mixItemListToList(moWallList)
    AirWalls = rdfGraph.getSubject(rdfGraph.pgd.hasSurfaceType, rdfGraph.moosas.AirWall)
    if AirWalls is not None:
        moWallList = np.append(moWallList, AirWalls)
    glsList = rdfGraph.getSubject(rdfGraph.pgd.hasSurfaceType, rdfGraph.moosas.Glazing)
    glsList = mixItemListToList(glsList)
    
    skyList = rdfGraph.getSubject(rdfGraph.pgd.hasSurfaceType, rdfGraph.moosas.Skylight)
    skyList = mixItemListToList(skyList)
    AirSkylights = rdfGraph.getSubject(rdfGraph.pgd.hasSurfaceType, rdfGraph.moosas.AirSkylight)
    if AirSkylights is not None:
        skyList = np.append(skyList, AirSkylights)
    shadingList = mixItemListToList(
        rdfGraph.getSubject(rdfGraph.pgd.hasSurfaceType, rdfGraph.moosas.Shading)
    )
    pgList = rdfGraph.getSubject(rdfGraph.rdf.type, rdfGraph.moosas.Program)
    pgList = mixItemListToList(pgList)
    spList = rdfGraph.getSubject(rdfGraph.rdf.type, rdfGraph.bot.Space)
    spList = sorted(mixItemListToList(spList), key=str)
    print()

    # construct geometryList
    model.geometryList = []
    for i, geoUri in enumerate(geoList):
        model.geometryList.append(rdfGraph.decodeGeo(geoUri))
        print(f'\rLOADING: Geometry {i + 1}/{len(geoList)}', end='')
    model.geoId = [geo.faceId for geo in model.geometryList]
    model.newIndex = len(model.geometryList)
    print()

    for shading_uri in shadingList:
        if shading_uri is None:
            continue
        shading_uri = URIRef(str(shading_uri))
        uid = str(rdfGraph.getObject(shading_uri, rdfGraph.moosas.Uid))
        geometry_uris = mixItemListToList(
            rdfGraph.getObject(shading_uri, rdfGraph.moosas.hasFace)
        )
        face_ids = [
            str(rdfGraph.getObject(URIRef(str(uri)), rdfGraph.moosas.faceId))
            for uri in geometry_uris
        ]
        model.shadingList.append(MoosasElement(model, face_ids, uid=uid))
        for key in rdfGraph.objects(shading_uri, rdfGraph.moosas.hasSetting):
            raw = next(rdfGraph.objects(shading_uri, rdfGraph.moosas[str(key)]), None)
            model.shadingList[-1].settings[str(key)] = _literal_to_python(raw)
        model.shadingList[-1].explicit_settings = {
            str(key) for key in rdfGraph.objects(shading_uri, rdfGraph.moosas.explicitSetting)
        }

    # construct LevelList
    for i, levelUri in enumerate(levelList):
        levelUri = URIRef(str(levelUri))
        model.levelList.append(float(rdfGraph.getObject(levelUri, rdfGraph.moosas.altitute)))
        print(f'\rLOADING: level {i + 1}/{len(levelList)}', end='')
    model.levelList.sort()
    print()

    # construct MoosasFaceList
    for i, faceUri in enumerate(moFaceList):
        element = rdfGraph.decodeElement(faceUri, model)
        if element:
            model.faceList.append(element)
        print(f'\rLOADING: Faces {i + 1}/{len(moFaceList)}', end='')
    print()

    # construct MoosasWallList
    for i, faceUri in enumerate(moWallList):
        element = rdfGraph.decodeElement(faceUri, model)
        if element:
            model.wallList.append(element)
        print(f'\rLOADING: Wall {i + 1}/{len(moWallList)}', end='')
    print()

    # construct MoosasGlazingList
    for i, faceUri in enumerate(glsList):
        if faceUri is not None:
            element = rdfGraph.decodeElement(faceUri, model)
            if element:
                parentFace = _first_or_none(rdfGraph.getSubject(rdfGraph.bot.hasSubElement, URIRef(faceUri)))
                parentFace = rdfGraph.decodeElement(str(parentFace), model) if parentFace else None
                if parentFace:
                    parentFace.add_glazing(element)
                model.glazingList.append(element)
        print(f'\rLOADING: glazing {i + 1}/{len(glsList)}', end='')
    print()

    # construct MoosasSkylightList
    for i, faceUri in enumerate(skyList):
        if faceUri is not None:
            element = rdfGraph.decodeElement(faceUri, model)
            if element:
                parentFace = _first_or_none(rdfGraph.getSubject(rdfGraph.bot.hasSubElement, URIRef(faceUri)))
                parentFace = rdfGraph.decodeElement(str(parentFace), model) if parentFace else None
                if parentFace:
                    parentFace.add_glazing(element)
                model.skylightList.append(element)
            print(f'\rLOADING: skylight {i + 1}/{len(skyList)}', end='')
    print()

    # load Building Template
    # for i, pgUri in enumerate(pgList):
    #     pgName = str(rdfGraph.getObject(URIRef(pgUri), rdfGraph.moosas.Uid))
    #     pgDict = {}
    #     for zInfo in rdfGraph.getSubject(rdfGraph.rdf.type, rdfGraph.moosas.ZoneInfo):
    #         zInfoName = rdfGraph.getObject(URIRef(zInfo), rdfGraph.moosas.Uid)
    #         pgDict[zInfoName] = str(rdfGraph.getObject(URIRef(pgUri), URIRef(zInfo)))
    #     model.includeTemplate(pgName, pgDict)
    #     print(f'\rLOADING: program {i + 1}/{len(pgList)}', end='')
    # print()
    # load Space
    loaded_spaces = []
    for i, spaceUri in enumerate(spList):
        spaceUri = URIRef(str(spaceUri))
        topology = {"Floor": [], "Ceiling": [], "Edge": []}

        # New format: Space --interfaceOf--> Interface --interfaceOf--> element_*
        interfaceList = mixItemListToList(rdfGraph.getSubject(rdfGraph.bot.interfaceOf, spaceUri))
        Uid = str(mixItemListToList(rdfGraph.getObject(spaceUri, rdfGraph.moosas.Uid))[0])
        for interfaceUri in interfaceList:
            interfaceUri = URIRef(str(interfaceUri))
            topoElementTypeRaw = _first_or_none(rdfGraph.getObject(interfaceUri, rdfGraph.pgd.hasSurfaceType))
            topoElementType = URIRef(str(topoElementTypeRaw)) if topoElementTypeRaw is not None else None

            linkedObjects = mixItemListToList(rdfGraph.getObject(interfaceUri, rdfGraph.bot.interfaceOf))
            elementUris = [URIRef(str(obj)) for obj in linkedObjects if str(obj) != str(spaceUri)]
            if topoElementType in (rdfGraph.moosas.Floor, rdfGraph.moosas.Ceiling):
                allowed_types = {rdfGraph.moosas.Face}
            elif topoElementType == rdfGraph.moosas.Edge:
                allowed_types = {rdfGraph.moosas.Wall, rdfGraph.moosas.AirWall}
            else:
                continue
            elementUris = [uri for uri in elementUris
                           if _first_or_none(rdfGraph.getObject(uri, rdfGraph.pgd.hasSurfaceType)) in allowed_types]
            if len(elementUris) == 0:
                continue
            if len(elementUris) != 1:
                raise ValueError(f"Ambiguous {topoElementType} topology interface: {interfaceUri}")
            element = rdfGraph.decodeElement(elementUris[0], model)
            if not element:
                continue

            if topoElementType == rdfGraph.moosas.Floor:
                topology["Floor"].append(element)
            elif topoElementType == rdfGraph.moosas.Ceiling:
                topology["Ceiling"].append(element)
            elif topoElementType == rdfGraph.moosas.Edge:
                orderVal = _first_or_none(rdfGraph.getObject(interfaceUri, rdfGraph.moosas.subElementOrder))
                order = int(float(orderVal)) if orderVal is not None else 0
                topology["Edge"].append((order, element))

        floorTopo = MoosasFloor(topology["Floor"]) if len(topology["Floor"]) > 0 else None
        ceilTopo = MoosasFloor(topology["Ceiling"]) if len(topology["Ceiling"]) > 0 else None
        edgeElements = [ele for _, ele in sorted(topology["Edge"], key=lambda x: x[0])]
        if len(edgeElements) == 0 and floorTopo is None and ceilTopo is None:
            print(f'\rLOADING: space {i + 1}/{len(spList)} skipped empty topology', end='')
            continue
        boundary_wkt = _first_or_none(
            rdfGraph.getObject(spaceUri, rdfGraph.moosas.boundaryWKT)
        )
        boundary = shapely.from_wkt(str(boundary_wkt)) if boundary_wkt is not None else None
        edgeTopo = MoosasEdge(edgeElements, boundary=boundary, owner_id=Uid)

        spc = MoosasSpace(
            _floor=floorTopo,
            _ceiling=ceilTopo,
            _edge=edgeTopo,
            Uid=Uid,
            space_type=_decode_space_type(rdfGraph, spaceUri),
            conditioned=_decode_conditioned(rdfGraph, spaceUri),
        )
        _decode_space_settings(rdfGraph, spaceUri, spc)

        loaded_spaces.append((spaceUri, spc))
        print(f'\rLOADING: space {i + 1}/{len(spList)}', end='')
    print()

    spaces_by_uri = {str(space_uri): space for space_uri, space in loaded_spaces}
    void_uris = set()
    for space_uri, space in loaded_spaces:
        contained = rdfGraph.getObject(space_uri, rdfGraph.bot.containsZone)
        if contained is None:
            continue
        for void_uri in mixItemListToList(contained):
            void = spaces_by_uri.get(str(void_uri))
            if void is not None:
                space.add_void(void)
                void_uris.add(str(void_uri))

    for space_uri, space in loaded_spaces:
        if str(space_uri) in void_uris or space.is_void():
            model.voidList.append(space)
        else:
            model.spaceList.append(space)

    # A sloping wall/end panel may be a ceiling interface for the room below
    # while remaining an Edge in its own room. That secondary use does not
    # belong in a MoosasFloor topology, but it is still a real Space user.
    active_ids = {space.id for space in model.spaceList}
    active_spaces = {str(uri): space for uri, space in loaded_spaces
                     if space.id in active_ids}
    for element in list(model.wallList) + list(model.faceList):
        element_uris = mixItemListToList(rdfGraph.getSubject(rdfGraph.moosas.Uid, Literal(element.Uid)))
        for element_uri in element_uris:
            element_uri = URIRef(str(element_uri))
            for interface_uri in mixItemListToList(rdfGraph.getSubject(rdfGraph.bot.interfaceOf, element_uri)):
                interface_uri = URIRef(str(interface_uri))
                for space_uri in mixItemListToList(rdfGraph.getObject(interface_uri, rdfGraph.bot.interfaceOf)):
                    space = active_spaces.get(str(space_uri))
                    if space is not None and space.id not in element.space:
                        element.space.append(space.id)
                    if (space is not None and isinstance(element,MoosasWall) and
                        _first_or_none(rdfGraph.getObject(interface_uri,rdfGraph.pgd.hasSurfaceType))==rdfGraph.moosas.Ceiling):
                        refs=space.settings.setdefault('secondary_ceiling_element_ids',[])
                        if element.Uid not in refs:refs.append(element.Uid)

    # Space settings are available only after the topology pass. Apply legacy
    # U/SHGC fallbacks now, while preserving all RDF-supplied element settings.
    _apply_element_parameter_defaults(model, rdfGraph)
    from ..metadata import apply_energy_metadata
    metadata = next(rdfGraph.objects(URIRef('urn:moosas:energy-model'), URIRef('urn:moosas:energy-metadata')), None)
    apply_energy_metadata(model, str(metadata) if metadata is not None else None,
                          include_settings=False)

    from .gbxml.energy import from_graph
    from_graph(model, rdfGraph)
    del rdfGraph._decode_element_cache
    return model
