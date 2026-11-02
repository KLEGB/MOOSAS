"""RDF <-> gbXML conversion utilities for MOOSAS.

This module implements an end-to-end conversion workflow between the BOT /
MOOSAS RDF representation used by :mod:`MoosasPy.model.io.rdf` and a compact gbXML
representation focused on spaces, surfaces, openings and planar geometry.

The converter is intentionally graph based.  It does not require constructing a
``MoosasModel`` instance, so it can be used as an independent workflow boundary
for RDF-centric pipelines.
"""
from __future__ import annotations


from dataclasses import dataclass, field
from pathlib import Path
import re
import math
import tempfile
from copy import deepcopy
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
import xml.etree.ElementTree as etree

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS, XSD
from shapely import wkt as shapely_wkt


BOT = Namespace("https://w3id.org/bot#")
GEO = Namespace("http://www.opengis.net/ont/geosparql#")
MOOSAS = Namespace("https://moosas#")
BES = Namespace("http://www.hkust.edu.hk/zhaojiwu/performance_based_generative_design#")
WGS = Namespace("https://www.w3.org/2003/01/geo/wgs84_pos#")
GBXML_NS = "http://www.gbxml.org/schema"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"


@dataclass
class GbxmlOpening:
    """Opening DTO used by the RDF to gbXML converter."""

    uri: URIRef
    gbxml_id: str
    name: str
    opening_type: str
    coords: List[Tuple[float, float, float]]
    properties: dict = field(default_factory=dict)


@dataclass
class GbxmlSurface:
    """Surface DTO used by the RDF to gbXML converter."""

    uri: URIRef
    gbxml_id: str
    name: str
    surface_type: str
    outside_boundary_condition: str
    coords: List[Tuple[float, float, float]]
    adjacent_space_ids: List[str] = field(default_factory=list)
    openings: List[GbxmlOpening] = field(default_factory=list)
    properties: dict = field(default_factory=dict)


@dataclass
class GbxmlSpace:
    """Space DTO used by the RDF to gbXML converter."""

    uri: URIRef
    gbxml_id: str
    name: str
    area: Optional[float] = None
    volume: Optional[float] = None
    north: Optional[float] = None


def _q(name: str) -> str:
    return f"{{{GBXML_NS}}}{name}"


def _local_name(value) -> str:
    """Return the local part of a URIRef or literal-like object."""

    if value is None:
        return ""
    text = str(value)
    if "#" in text:
        return text.rsplit("#", 1)[-1]
    if "/" in text:
        return text.rstrip("/").rsplit("/", 1)[-1]
    return text


def _safe_xml_id(value, prefix: str = "id") -> str:
    """Create a stable XML-id-like string while preserving as much source ID as possible."""

    raw = _local_name(value) or prefix
    raw = re.sub(r"[^A-Za-z0-9_.:-]", "_", raw)
    if not raw or not re.match(r"^[A-Za-z_]", raw):
        raw = f"{prefix}_{raw}"
    return raw


def _dedupe_id(base: str, used: set[str]) -> str:
    candidate = base
    idx = 1
    while candidate in used:
        idx += 1
        candidate = f"{base}_{idx}"
    used.add(candidate)
    return candidate


def _first(graph: Graph, subject, predicate):
    for obj in graph.objects(subject, predicate):
        return obj
    return None


def _literal_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _format_float(value: float) -> str:
    text = f"{float(value):.10f}".rstrip("0").rstrip(".")
    return text if text else "0"


def _polygon_coords_from_wkt(wkt_text: str, close: bool = False) -> List[Tuple[float, float, float]]:
    """Parse a GeoSPARQL WKT polygon into gbXML CartesianPoint coordinates."""

    geom = shapely_wkt.loads(str(wkt_text))
    if geom.geom_type == "MultiPolygon":
        geom = list(geom.geoms)[0]
    if geom.geom_type != "Polygon":
        raise ValueError(f"Only POLYGON/MultiPolygon WKT is supported, got {geom.geom_type}")
    coords: List[Tuple[float, float, float]] = []
    for coord in geom.exterior.coords:
        if len(coord) >= 3:
            x, y, z = coord[:3]
        else:
            x, y = coord[:2]
            z = 0.0
        coords.append((float(x), float(y), float(z)))
    if coords and coords[0] == coords[-1] and not close:
        coords = coords[:-1]
    if coords and coords[0] != coords[-1] and close:
        coords.append(coords[0])
    return coords


def _coords_to_wkt(coords: Sequence[Tuple[float, float, float]]) -> str:
    """Serialize gbXML PolyLoop coordinates to POLYGON Z WKT."""

    points = list(coords)
    if points and points[0] != points[-1]:
        points.append(points[0])
    payload = ", ".join(
        f"{_format_float(x)} {_format_float(y)} {_format_float(z)}" for x, y, z in points
    )
    return f"POLYGON Z (({payload}))"


def _get_wkt_literals(graph: Graph, node) -> List[str]:
    """Find WKT literals directly on a node or via geo:hasGeometry / moosas:hasFace."""

    wkts: List[str] = []
    for wkt_literal in graph.objects(node, GEO.asWKT):
        wkts.append(str(wkt_literal))
    for geom_node in graph.objects(node, GEO.hasGeometry):
        for wkt_literal in graph.objects(geom_node, GEO.asWKT):
            wkts.append(str(wkt_literal))
    for face_node in graph.objects(node, MOOSAS.hasFace):
        wkts.extend(_get_wkt_literals(graph, face_node))
    return wkts


def _get_first_coords(graph: Graph, node) -> Optional[List[Tuple[float, float, float]]]:
    for wkt_text in _get_wkt_literals(graph, node):
        try:
            return _polygon_coords_from_wkt(wkt_text, close=False)
        except Exception:
            continue
    return None


def _is_subject_of_type(graph: Graph, node, rdf_type) -> bool:
    return (node, RDF.type, rdf_type) in graph


def _surface_type_from(graph: Graph, element, interfaces: Sequence[URIRef] = ()) -> Optional[str]:
    """Read gbXML-like surface type from Interface first, then Element."""

    for iface in interfaces:
        for pred in (BES.surfaceType, BES.hasSurfaceType):
            value = _first(graph, iface, pred)
            if value is not None:
                return _local_name(value)
    for pred in (BES.surfaceType, BES.hasSurfaceType):
        value = _first(graph, element, pred)
        if value is not None:
            return _local_name(value)
    uid = _local_name(_first(graph, element, MOOSAS.Uid)) or _local_name(element)
    if uid.startswith("wall_"):
        return "ExteriorWall"
    if uid.startswith("face_"):
        return "Roof"
    if uid.startswith("gls_"):
        return "OperableWindow"
    if uid.startswith("sky_"):
        return "FixedSkylight"
    return None


def _normalize_surface_type(surface_type: Optional[str], has_surface_type: Optional[str] = None) -> str:
    st = surface_type or has_surface_type or "ExteriorWall"
    mapping = {
        "Wall": "ExteriorWall",
        "Face": "Roof",
        "Edge": "ExteriorWall",
        "Floor": "SlabOnGrade",
        "Ceiling": "Roof",
        "Glazing": "FixedWindow",
        "Skylight": "FixedSkylight",
        "AirSkylight": "Air",
        "AirWall": "Air",
    }
    return mapping.get(st, st)


def _is_opening_type(surface_type: Optional[str]) -> bool:
    if not surface_type:
        return False
    return surface_type in {
        "FixedWindow",
        "OperableWindow",
        "FixedSkylight",
        "OperableSkylight",
        "AirWindow",
        "Glazing",
        "Skylight",
        "AirSkylight",
        "OpeningAir",
        "SlidingDoor",
        "NonSlidingDoor",
    } or "Window" in surface_type or "Skylight" in surface_type


def _normalize_opening_type(opening_type: Optional[str]) -> str:
    ot = opening_type or "FixedWindow"
    mapping = {
        "Glazing": "FixedWindow",
        "Skylight": "FixedSkylight",
        "AirSkylight": "Air",
        "AirWindow": "Air",
    }
    return mapping.get(ot, ot)


def _outside_boundary_condition(graph: Graph, element, surface_type: str, adjacent_count: int) -> str:
    raw = _first(graph, element, BES.hasOutsideBoundaryCondition)
    if raw is not None:
        condition = str(raw)
        if condition.lower() in {"indoors", "indoor", "interior"}:
            return "Surface"
        if condition:
            return condition
    if adjacent_count > 1 or surface_type in {"InteriorWall", "InteriorFloor", "Ceiling"}:
        return "Surface"
    if surface_type in {"SlabOnGrade", "UndergroundSlab", "UndergroundWall"}:
        return "Ground"
    return "Outdoors"


def _collect_spaces(graph: Graph) -> Tuple[List[GbxmlSpace], Dict[URIRef, str]]:
    used: set[str] = set()
    spaces: List[GbxmlSpace] = []
    id_map: Dict[URIRef, str] = {}
    for space_uri in sorted(graph.subjects(RDF.type, BOT.Space), key=str):
        uid = _first(graph, space_uri, MOOSAS.Uid)
        gbxml_id = _dedupe_id(_safe_xml_id(uid or space_uri, "space"), used)
        name = str(uid) if uid is not None else _local_name(space_uri)
        sp = GbxmlSpace(
            uri=space_uri,
            gbxml_id=gbxml_id,
            name=name,
            area=_literal_float(_first(graph, space_uri, BES.hasFloorArea_m2)),
            volume=_literal_float(_first(graph, space_uri, BES.hasVolume_m3)),
            north=_literal_float(_first(graph, space_uri, BES.hasNorthDirection_deg)),
        )
        spaces.append(sp)
        id_map[space_uri] = gbxml_id
    return spaces, id_map


def _collect_interface_links(graph: Graph):
    element_to_spaces: Dict[URIRef, set[URIRef]] = {}
    element_to_interfaces: Dict[URIRef, List[URIRef]] = {}
    space_to_elements: Dict[URIRef, set[URIRef]] = {}
    for iface in graph.subjects(RDF.type, BOT.Interface):
        objects = list(graph.objects(iface, BOT.interfaceOf))
        spaces = [obj for obj in objects if _is_subject_of_type(graph, obj, BOT.Space)]
        elements = [obj for obj in objects if _is_subject_of_type(graph, obj, BOT.Element)]
        for element in elements:
            element_to_interfaces.setdefault(element, []).append(iface)
            element_to_spaces.setdefault(element, set()).update(spaces)
            for space in spaces:
                space_to_elements.setdefault(space, set()).add(element)
    # Fallback for RDF documents that use direct adjacency instead of bot:Interface.
    for space in graph.subjects(RDF.type, BOT.Space):
        for pred in (BOT.adjacentElement, MOOSAS.hasFace, BOT.hasElement):
            for element in graph.objects(space, pred):
                if _is_subject_of_type(graph, element, BOT.Element):
                    element_to_spaces.setdefault(element, set()).add(space)
                    space_to_elements.setdefault(space, set()).add(element)
    return element_to_spaces, element_to_interfaces, space_to_elements


def _opening_from_element(
    graph: Graph,
    opening_element,
    used_ids: set[str],
) -> Optional[GbxmlOpening]:
    coords = _get_first_coords(graph, opening_element)
    if not coords:
        return None
    opening_type = _normalize_opening_type(_surface_type_from(graph, opening_element, ()))
    uid = _first(graph, opening_element, MOOSAS.Uid)
    gbxml_id = _dedupe_id(_safe_xml_id(uid or opening_element, "opening"), used_ids)
    return GbxmlOpening(
        uri=opening_element,
        gbxml_id=gbxml_id,
        name=str(uid) if uid is not None else _local_name(opening_element),
        opening_type=opening_type,
        coords=coords,
        properties=_thermal_properties(graph, opening_element),
    )


def _opening_from_hole(
    graph: Graph,
    parent_surface,
    hole_node,
    used_ids: set[str],
) -> Optional[GbxmlOpening]:
    coords = _get_first_coords(graph, hole_node)
    if not coords:
        return None
    base = f"{_safe_xml_id(parent_surface, 'surface')}_{_safe_xml_id(hole_node, 'hole')}"
    gbxml_id = _dedupe_id(_safe_xml_id(base, "opening"), used_ids)
    return GbxmlOpening(
        uri=hole_node,
        gbxml_id=gbxml_id,
        name=_local_name(hole_node),
        opening_type="FixedWindow",
        coords=coords,
    )


def _collect_surfaces(graph: Graph, space_id_map: Dict[URIRef, str]) -> List[GbxmlSurface]:
    element_to_spaces, element_to_interfaces, _ = _collect_interface_links(graph)
    used_surface_ids: set[str] = set()
    used_opening_ids: set[str] = set()
    surfaces: List[GbxmlSurface] = []

    for element in sorted(graph.subjects(RDF.type, BOT.Element), key=str):
        if any(graph.subjects(BOT.hasSubElement, element)):
            continue
        iface_list = element_to_interfaces.get(element, [])
        raw_surface_type = _surface_type_from(graph, element, iface_list)
        if _is_opening_type(raw_surface_type):
            continue
        coords = _get_first_coords(graph, element)
        if not coords:
            continue
        surface_type = _normalize_surface_type(raw_surface_type)
        adjacent_spaces = [space_id_map[sp] for sp in sorted(element_to_spaces.get(element, set()), key=str) if sp in space_id_map]
        uid = _first(graph, element, MOOSAS.Uid)
        gbxml_id = _dedupe_id(_safe_xml_id(uid or element, "surface"), used_surface_ids)
        surface = GbxmlSurface(
            uri=element,
            gbxml_id=gbxml_id,
            name=str(uid) if uid is not None else _local_name(element),
            surface_type=surface_type,
            outside_boundary_condition=_outside_boundary_condition(graph, element, surface_type, len(adjacent_spaces)),
            coords=coords,
            adjacent_space_ids=adjacent_spaces,
            properties=_thermal_properties(graph, element),
        )

        # Explicit parent-child window/skylight relations from the MOOSAS RDF encoder.
        seen_opening_uris: set[URIRef] = set()
        seen_opening_coords = set()
        for opening_element in graph.objects(element, BOT.hasSubElement):
            opening = _opening_from_element(graph, opening_element, used_opening_ids)
            if opening is not None:
                surface.openings.append(opening)
                seen_opening_uris.add(opening_element)
                seen_opening_coords.add(tuple(opening.coords))

        # Geometry-level holes are preserved even when no separate opening element exists.
        for face_node in graph.objects(element, MOOSAS.hasFace):
            for hole_node in graph.objects(face_node, MOOSAS.hasHole):
                opening = _opening_from_hole(graph, element, hole_node, used_opening_ids)
                if opening is not None and opening.uri not in seen_opening_uris and tuple(opening.coords) not in seen_opening_coords:
                    surface.openings.append(opening)
                    seen_opening_coords.add(tuple(opening.coords))
        for hole_node in graph.objects(element, MOOSAS.hasHole):
            opening = _opening_from_hole(graph, element, hole_node, used_opening_ids)
            if opening is not None and opening.uri not in seen_opening_uris and tuple(opening.coords) not in seen_opening_coords:
                surface.openings.append(opening)
                seen_opening_coords.add(tuple(opening.coords))

        surfaces.append(surface)
    # A Moosas element can own multiple geometries. Export each part rather
    # than silently taking its first face, and attach openings to their plane.
    expanded = []
    for surface in surfaces:
        parts = []
        for text in _get_wkt_literals(graph, surface.uri):
            geom = shapely_wkt.loads(text)
            for polygon in (geom.geoms if geom.geom_type == 'MultiPolygon' else [geom]):
                coords = _polygon_coords_from_wkt(polygon.wkt)
                if coords not in parts:
                    parts.append(coords)
        if len(parts) <= 1:
            expanded.append(surface)
            continue
        for i, coords in enumerate(parts):
            part = deepcopy(surface)
            part.coords = coords
            if i:
                part.gbxml_id = _dedupe_id(surface.gbxml_id+f'_part{i+1}', used_surface_ids)
            origin, u, v, normal, _, _ = _rectangle_frame(coords)
            from shapely.geometry import Polygon, Point
            project = lambda p: (sum((p[j]-origin[j])*u[j] for j in range(3)), sum((p[j]-origin[j])*v[j] for j in range(3)))
            polygon = Polygon([project(p) for p in coords]).buffer(1e-5)
            part.openings = []
            for opening in surface.openings:
                if not all(abs(sum((p[j]-origin[j])*normal[j] for j in range(3))) < 1e-4 for p in opening.coords):
                    continue
                center = [sum(p[j] for p in opening.coords)/len(opening.coords) for j in range(3)]
                if polygon.covers(Point(project(center))):
                    part.openings.append(deepcopy(opening))
            expanded.append(part)
    return expanded


def _thermal_properties(graph, subject):
    result = {}
    for key, predicates in {'u_value': (MOOSAS.u_value, MOOSAS.U_Value),
                            'thickness': (MOOSAS.thickness,), 'volumetric_heat_capacity': (MOOSAS.volumetric_heat_capacity,),
                            'transparency': (MOOSAS.transparency,), 'reflection': (MOOSAS.reflection,),
                            'shgc': (MOOSAS.shgc, MOOSAS.SHGC)}.items():
        for pred in predicates:
            value = _literal_float(_first(graph, subject, pred))
            if value is not None:
                result[key] = value
                break
    return result


def _rectangle_frame(coords):
    normal = [0., 0., 0.]
    for a, b in zip(coords, list(coords[1:])+[coords[0]]):
        normal[0] += (a[1]-b[1])*(a[2]+b[2])
        normal[1] += (a[2]-b[2])*(a[0]+b[0])
        normal[2] += (a[0]-b[0])*(a[1]+b[1])
    length = math.sqrt(sum(n*n for n in normal))
    if length < 1e-10:
        raise ValueError('Cannot export a degenerate gbXML polygon')
    normal = [n/length for n in normal]
    u = [-normal[1], normal[0], 0] if abs(normal[2]) < .999 else [1., 0., 0.]
    length = math.sqrt(sum(n*n for n in u)); u = [n/length for n in u]
    v = [normal[1]*u[2]-normal[2]*u[1], normal[2]*u[0]-normal[0]*u[2], normal[0]*u[1]-normal[1]*u[0]]
    if v[2] < 0:
        u = [-x for x in u]; v = [-x for x in v]
    xs = [sum(p[j]*u[j] for j in range(3)) for p in coords]
    ys = [sum(p[j]*v[j] for j in range(3)) for p in coords]
    anchor = coords[0]
    dx, dy = min(xs)-xs[0], min(ys)-ys[0]
    origin = [anchor[j]+dx*u[j]+dy*v[j] for j in range(3)]
    return origin, u, v, normal, max(xs)-min(xs), max(ys)-min(ys)


def _add_rectangular(parent, coords, parent_frame=None):
    origin, u, v, normal, width, height = _rectangle_frame(coords)
    rectangular = etree.SubElement(parent, _q('RectangularGeometry'))
    if parent_frame is None:
        _add_text(rectangular, 'Azimuth', _format_float(math.degrees(math.atan2(normal[0],normal[1])) % 360))
        point = etree.SubElement(rectangular, _q('CartesianPoint'))
        for value in origin:
            _add_text(point, 'Coordinate', _format_float(value))
        _add_text(rectangular, 'Tilt', _format_float(math.degrees(math.acos(min(1.,abs(normal[2]))))))
    else:
        base, pu, pv, *_ = parent_frame
        point = etree.SubElement(rectangular, _q('CartesianPoint'))
        for axis in (pu, pv):
            _add_text(point, 'Coordinate', _format_float(min(sum((p[j]-base[j])*axis[j] for j in range(3)) for p in coords)))
    _add_text(rectangular, 'Width', _format_float(width))
    _add_text(rectangular, 'Height', _format_float(height))


def _add_text(parent, tag: str, text) -> Optional[etree.Element]:
    if text is None:
        return None
    child = etree.SubElement(parent, _q(tag))
    child.text = str(text)
    return child


def _add_polyloop(parent, coords: Sequence[Tuple[float, float, float]]):
    planar = etree.SubElement(parent, _q("PlanarGeometry"))
    polyloop = etree.SubElement(planar, _q("PolyLoop"))
    for x, y, z in coords:
        point = etree.SubElement(polyloop, _q("CartesianPoint"))
        _add_text(point, "Coordinate", _format_float(x))
        _add_text(point, "Coordinate", _format_float(y))
        _add_text(point, "Coordinate", _format_float(z))
    return polyloop


def parse_rdf_for_gbxml(input_path: str | Path, rdf_format: str = "turtle") -> Tuple[List[GbxmlSpace], List[GbxmlSurface]]:
    """Parse RDF and return gbXML-oriented DTOs without writing files."""

    graph = Graph()
    graph.parse(str(input_path), format=rdf_format)
    spaces, space_id_map = _collect_spaces(graph)
    surfaces = _collect_surfaces(graph, space_id_map)
    return spaces, surfaces


def build_gbxml_tree(
    spaces: Sequence[GbxmlSpace],
    surfaces: Sequence[GbxmlSurface],
    building_id: str = "building-1",
    campus_id: str = "campus-1",
    building_type: str = "Unknown",
    gbxml_version: str = "0.37",
) -> etree.ElementTree:
    """Build an ElementTree in the gbXML namespace from parsed DTOs."""

    etree.register_namespace("", GBXML_NS)
    etree.register_namespace("xsi", XSI_NS)
    root = etree.Element(
        _q("gbXML"),
        attrib={
            "version": gbxml_version,
            "useSIUnitsForResults": "true",
            "lengthUnit": "Meters",
            "areaUnit": "SquareMeters",
            "volumeUnit": "CubicMeters",
            "temperatureUnit": "C",
        },
    )
    # A construction without layers/material references cannot be resolved by
    # legacy importers. Use a complete, explicitly generic fallback construction.
    material = etree.SubElement(root, _q('Material'), id='material-default')
    _add_text(material, 'Name', 'Generic opaque material')
    for tag, value, unit in [('Thickness', '0.1', 'Meters'), ('Conductivity', '0.5', 'WPerMeterK'),
                             ('Density', '1000', 'KgPerCubicM'), ('SpecificHeat', '1000', 'JPerKgK')]:
        etree.SubElement(material, _q(tag), unit=unit).text = value
    layer = etree.SubElement(root, _q('Layer'), id='layer-default')
    etree.SubElement(layer, _q('MaterialId'), materialIdRef='material-default')
    construction = etree.SubElement(root, _q('Construction'), id='construction-default')
    _add_text(construction, 'Name', 'Generic opaque construction')
    etree.SubElement(construction, _q('LayerId'), layerIdRef='layer-default')
    window_type = etree.SubElement(root, _q('WindowType'), id='window-default')
    _add_text(window_type, 'Name', 'Generic window')
    etree.SubElement(window_type, _q('U-value'), unit='WPerSquareMeterK').text = '2.4'
    construction_refs, window_refs = {}, {}
    opaque_cache, window_cache = {}, {}
    for surface in surfaces:
        properties = surface.properties
        u_value = properties.get('u_value')
        if u_value and u_value > 0:
            film = .14 if surface.surface_type == 'Roof' else .21 if surface.surface_type in {'SlabOnGrade', 'UndergroundSlab','RaisedFloor'} else .17
            thickness = properties.get('thickness', .2)
            capacity = properties.get('volumetric_heat_capacity', 1e6)
            resistance = max(1/u_value-film, .001)
            signature = (u_value, thickness, capacity, resistance)
            if signature not in opaque_cache:
                index = len(opaque_cache)+1
                mid, lid, cid = f'material-rdf-{index}', f'layer-rdf-{index}', f'construction-rdf-{index}'
                material = etree.SubElement(root, _q('Material'), id=mid)
                _add_text(material, 'Name', f'RDF equivalent material {index}')
                for tag, value, unit in [('Thickness', thickness, 'Meters'), ('Conductivity', thickness/resistance, 'WPerMeterK'),
                                         ('Density', 1000, 'KgPerCubicM'), ('SpecificHeat', capacity/1000, 'JPerKgK')]:
                    etree.SubElement(material, _q(tag), unit=unit).text = _format_float(value)
                layer = etree.SubElement(root, _q('Layer'), id=lid)
                etree.SubElement(layer, _q('MaterialId'), materialIdRef=mid)
                construction = etree.SubElement(root, _q('Construction'), id=cid)
                _add_text(construction, 'Name', f'RDF equivalent construction {index}')
                etree.SubElement(construction, _q('U-value'), unit='WPerSquareMeterK').text = _format_float(u_value)
                etree.SubElement(construction, _q('LayerId'), layerIdRef=lid)
                opaque_cache[signature] = cid
            construction_refs[surface.gbxml_id] = opaque_cache[signature]
        for opening in surface.openings:
            properties = opening.properties
            signature = (properties.get('u_value', 2.4), properties.get('transparency'))
            if signature not in window_cache:
                ident = f'window-rdf-{len(window_cache)+1}'
                window_type = etree.SubElement(root, _q('WindowType'), id=ident)
                _add_text(window_type, 'Name', f'RDF equivalent glazing {len(window_cache)+1}')
                etree.SubElement(window_type, _q('U-value'), unit='WPerSquareMeterK').text = _format_float(signature[0])
                if signature[1] is not None:
                    etree.SubElement(window_type, _q('Transmittance'), unit='Fraction', type='Solar').text = _format_float(signature[1])
                pane = etree.SubElement(window_type, _q('Glaze'), id=ident+'-pane')
                _add_text(pane, 'Name', 'Equivalent glazing pane')
                etree.SubElement(pane, _q('Thickness'), unit='Meters').text = '0.006'
                window_cache[signature] = ident
            window_refs[opening.gbxml_id] = window_cache[signature]
    ranks = {_q('Material'):0, _q('Layer'):1, _q('Construction'):2, _q('WindowType'):3}
    root[:] = sorted(root, key=lambda node: ranks.get(node.tag, 4))
    campus = etree.SubElement(root, _q("Campus"), id=campus_id)
    building = etree.SubElement(campus, _q("Building"), id=building_id, buildingType=building_type)
    total_area = sum(space.area for space in spaces if space.area is not None)
    if total_area:
        _add_text(building, "Area", _format_float(total_area))

    space_nodes = {}
    for space in spaces:
        space_el = etree.SubElement(building, _q("Space"), id=space.gbxml_id)
        space_nodes[space.gbxml_id] = space_el
        _add_text(space_el, "Name", space.name)
        if space.area is not None:
            _add_text(space_el, "Area", _format_float(space.area))
        if space.volume is not None:
            _add_text(space_el, "Volume", _format_float(space.volume))

    for surface in surfaces:
        surface_el = etree.SubElement(
            campus,
            _q("Surface"),
            id=surface.gbxml_id,
            surfaceType=surface.surface_type,
            constructionIdRef=construction_refs.get(surface.gbxml_id, 'construction-default'),
        )
        _add_text(surface_el, "Name", surface.name)
        _add_text(surface_el, "Description", f"OutsideBoundaryCondition={surface.outside_boundary_condition}")
        for space_id in surface.adjacent_space_ids:
            etree.SubElement(surface_el, _q("AdjacentSpaceId"), spaceIdRef=space_id)
        _add_rectangular(surface_el, surface.coords)
        _add_polyloop(surface_el, surface.coords)
        for opening in surface.openings:
            opening_el = etree.SubElement(
                surface_el,
                _q("Opening"),
                id=opening.gbxml_id,
                openingType=opening.opening_type,
                windowTypeIdRef=window_refs.get(opening.gbxml_id, 'window-default'),
            )
            _add_text(opening_el, "Name", opening.name)
            _add_rectangular(opening_el, opening.coords, _rectangle_frame(surface.coords))
            _add_polyloop(opening_el, opening.coords)
    for sid, space_el in space_nodes.items():
        shell = etree.SubElement(space_el, _q('ShellGeometry'), id='shell-'+sid, unit='Meters')
        closed = etree.SubElement(shell, _q('ClosedShell'))
        for surface in surfaces:
            if sid not in surface.adjacent_space_ids:
                continue
            dummy = etree.Element('dummy')
            polyloop = _add_polyloop(dummy, surface.coords)
            closed.append(polyloop)
    return etree.ElementTree(root)


def convert_rdf_to_gbxml(
    input_path: str | Path,
    output_path: str | Path,
    rdf_format: str = "turtle",
    building_id: str = "building-1",
    campus_id: str = "campus-1",
    building_type: str = "Unknown",
    gbxml_version: str = "0.37",
) -> etree.ElementTree:
    """Convert a BOT/MOOSAS RDF Turtle file to gbXML.

    Parameters
    ----------
    input_path:
        RDF input file.  The MOOSAS exporter writes Turtle even when using the
        ``.rdf`` suffix, so the default format is ``turtle``.
    output_path:
        Destination gbXML path.
    rdf_format:
        rdflib parser format.  Defaults to ``turtle``.
    """

    from .exchange import restore_exchange, validate_references
    graph = Graph().parse(str(input_path), format=rdf_format)
    from .energy import DOC as THERMAL_DOC,GBE
    evidence = graph.value(THERMAL_DOC,GBE.projection)
    if evidence is not None:
        from .exchange import DOC as EXCHANGE_DOC,EX,capture_exchange
        if graph.value(EXCHANGE_DOC,EX.xmlSkeleton) is None:
            import json
            capture_exchange(etree.fromstring(json.loads(str(evidence))['source_xml']),graph)
    tree = restore_exchange(graph)
    if tree is None:
        spaces, space_id_map = _collect_spaces(graph)
        surfaces = _collect_surfaces(graph, space_id_map)
        tree = build_gbxml_tree(
            spaces,
            surfaces,
            building_id=building_id,
            campus_id=campus_id,
            building_type=building_type,
            gbxml_version=gbxml_version,
        )
    validate_references(tree.getroot())
    etree.indent(tree, space="  ")
    tree.write(str(output_path), xml_declaration=True, encoding="utf-8")
    return tree


def _findall(parent, tag: str):
    return parent.findall(f".//{{{GBXML_NS}}}{tag}")


def _find_child(parent, tag: str):
    return parent.find(f"{{{GBXML_NS}}}{tag}")


def _child_text(parent, tag: str) -> Optional[str]:
    child = _find_child(parent, tag)
    if child is None or child.text is None:
        return None
    return child.text.strip()


def _polyloop_coords(parent) -> List[Tuple[float, float, float]]:
    polyloop = parent.find(f".//{{{GBXML_NS}}}PlanarGeometry/{{{GBXML_NS}}}PolyLoop")
    if polyloop is None:
        return []
    coords: List[Tuple[float, float, float]] = []
    for point in polyloop.findall(f"{{{GBXML_NS}}}CartesianPoint"):
        nums = []
        for coord in point.findall(f"{{{GBXML_NS}}}Coordinate"):
            if coord.text is not None:
                nums.append(float(coord.text))
        if len(nums) == 2:
            nums.append(0.0)
        if len(nums) >= 3:
            coords.append((nums[0], nums[1], nums[2]))
    return coords


def _add_geometry_triples(graph: Graph, owner_uri: URIRef, geometry_id: str, coords: Sequence[Tuple[float, float, float]]):
    face_uri = URIRef(geometry_id)
    wkt_uri = URIRef(f"{geometry_id}fv")
    graph.add((owner_uri, MOOSAS.hasFace, face_uri))
    graph.add((face_uri, RDF.type, MOOSAS.Geometry))
    graph.add((face_uri, MOOSAS.faceId, Literal(geometry_id)))
    graph.add((face_uri, MOOSAS.Category, Literal(-1)))
    graph.add((face_uri, GEO.hasGeometry, wkt_uri))
    graph.add((wkt_uri, GEO.asWKT, Literal(_coords_to_wkt(coords), datatype=GEO.wktLiteral)))
    return face_uri


def convert_gbxml_to_rdf(
    input_path: str | Path,
    output_path: str | Path,
    rdf_format: str = "turtle",
    base_uri: str = "",
) -> Graph:
    """Convert gbXML spaces/surfaces/openings back to BOT/MOOSAS RDF."""

    def uri(identifier: str) -> URIRef:
        return URIRef(f"{base_uri}{identifier}") if base_uri else URIRef(identifier)

    tree = etree.parse(str(input_path))
    root = tree.getroot()
    from .energy import units
    length_scale, area_scale, volume_scale = units(root)

    graph = Graph()
    graph.bind("bes", BES)
    graph.bind("bot", BOT)
    graph.bind("geo", GEO)
    graph.bind("moosas", MOOSAS)
    graph.bind("rdf", RDF)
    graph.bind("rdfs", RDFS)
    graph.bind("xsd", XSD)
    graph.bind("wgs", WGS)

    # The native RDF loader expects at least one BOT storey and every model
    # element to point to it.  gbXML carries elevations in the geometry rather
    # than as a separate storey object, so use the building datum as level 0;
    # surface coordinates remain unchanged in the geometry triples.
    building_uri = uri("Building_1")
    level_uri = uri("Level_0.0")
    graph.add((building_uri, RDF.type, BOT.Building))
    graph.add((level_uri, RDF.type, BOT.Storey))
    graph.add((level_uri, MOOSAS.altitute, Literal(0.0, datatype=XSD.double)))
    graph.add((building_uri, BOT.hasStorey, level_uri))

    def ensure_level(level: float):
        level = round(float(level), 6)
        level_node = uri(f"Level_{level}")
        if (level_node, RDF.type, BOT.Storey) not in graph:
            graph.add((level_node, RDF.type, BOT.Storey))
            graph.add((level_node, MOOSAS.altitute, Literal(level, datatype=XSD.double)))
            graph.add((building_uri, BOT.hasStorey, level_node))
        return level_node

    for space_el in root.findall(f".//{{{GBXML_NS}}}Space"):
        sid = space_el.get("id")
        if not sid:
            continue
        space_uri = uri(f"Space_{sid}" if not sid.startswith("Space_") else sid)
        graph.add((space_uri, RDF.type, BOT.Space))
        graph.add((space_uri, MOOSAS.Uid, Literal(sid)))
        name = _child_text(space_el, "Name")
        if name:
            graph.add((space_uri, RDFS.label, Literal(name)))
        area = _child_text(space_el, "Area")
        if area is not None:
            graph.add((space_uri, BES.hasFloorArea_m2, Literal(float(area)*area_scale, datatype=XSD.double)))
        volume = _child_text(space_el, "Volume")
        if volume is not None:
            graph.add((space_uri, BES.hasVolume_m3, Literal(float(volume)*volume_scale, datatype=XSD.double)))
        graph.add((space_uri, BES.hasNorthDirection_deg, Literal(0.0, datatype=XSD.double)))

    edge_orders: Dict[URIRef, int] = {}
    for surface_el in root.findall(f".//{{{GBXML_NS}}}Surface"):
        surface_id = surface_el.get("id")
        if not surface_id:
            continue
        surface_uri = uri(f"element_{surface_id}" if not surface_id.startswith("element_") else surface_id)
        surface_type = surface_el.get("surfaceType") or "ExteriorWall"
        graph.add((surface_uri, RDF.type, BOT.Element))
        graph.add((surface_uri, MOOSAS.Uid, Literal(surface_id)))
        graph.add((surface_uri, MOOSAS.Offset, Literal(0.0, datatype=XSD.double)))
        graph.add((surface_uri, BES.surfaceType, BES.term(surface_type)))
        graph.add((surface_uri, BES.hasSurfaceType, MOOSAS.term("Wall" if "Wall" in surface_type else "Face")))
        obc = _child_text(surface_el, "OutsideBoundaryCondition")
        if not obc:
            desc = _child_text(surface_el, "Description")
            if desc and "OutsideBoundaryCondition=" in desc:
                obc = desc.split("OutsideBoundaryCondition=", 1)[1].split(";", 1)[0].strip()
        if obc:
            graph.add((surface_uri, BES.hasOutsideBoundaryCondition, Literal(obc)))
        coords = [tuple(v*length_scale for v in p) for p in _polyloop_coords(surface_el)]
        if coords:
            face_uri = _add_geometry_triples(graph, surface_uri, surface_id, coords)
            surface_level = ensure_level(min(point[2] for point in coords))
            graph.add((surface_uri, MOOSAS.hasLevel, surface_level))
        else:
            face_uri = None

        for adj in surface_el.findall(f"{{{GBXML_NS}}}AdjacentSpaceId"):
            space_ref = adj.get("spaceIdRef")
            if not space_ref:
                continue
            space_uri = uri(f"Space_{space_ref}" if not space_ref.startswith("Space_") else space_ref)
            iface_uri = uri(f"{space_ref}_{surface_id}")
            graph.add((iface_uri, RDF.type, BOT.Interface))
            graph.add((iface_uri, BOT.interfaceOf, space_uri))
            graph.add((iface_uri, BOT.interfaceOf, surface_uri))
            # The RDF loader uses MOOSAS topology classes to assemble spaces;
            # retain the original gbXML type on the interface as well.
            if surface_type in {"Floor", "SlabOnGrade", "UndergroundSlab", "InteriorFloor", "RaisedFloor"}:
                topology_type = MOOSAS.Floor
            elif surface_type in {"Roof", "Ceiling"}:
                topology_type = MOOSAS.Ceiling
            else:
                topology_type = MOOSAS.Edge
            graph.add((iface_uri, BES.hasSurfaceType, topology_type))
            graph.add((iface_uri, BES.surfaceType, BES.term(surface_type)))
            if topology_type == MOOSAS.Edge:
                order = edge_orders.get(space_uri, 0)
                graph.add((iface_uri, MOOSAS.subElementOrder, Literal(order, datatype=XSD.integer)))
                edge_orders[space_uri] = order + 1
            graph.add((space_uri, BOT.adjacentElement, surface_uri))

        for opening_el in surface_el.findall(f"{{{GBXML_NS}}}Opening"):
            opening_id = opening_el.get("id")
            if not opening_id:
                continue
            opening_uri = uri(f"element_{opening_id}" if not opening_id.startswith("element_") else opening_id)
            opening_type = opening_el.get("openingType") or "FixedWindow"
            graph.add((opening_uri, RDF.type, BOT.Element))
            graph.add((opening_uri, MOOSAS.Uid, Literal(opening_id)))
            graph.add((opening_uri, MOOSAS.Offset, Literal(0.0, datatype=XSD.double)))
            graph.add((opening_uri, BES.surfaceType, BES.term(opening_type)))
            graph.add((opening_uri, BES.hasSurfaceType, MOOSAS.term("Skylight" if "Skylight" in opening_type else "Glazing")))
            graph.add((surface_uri, BOT.hasSubElement, opening_uri))
            opening_coords = [tuple(v*length_scale for v in p) for p in _polyloop_coords(opening_el)]
            if opening_coords:
                opening_face_uri = _add_geometry_triples(graph, opening_uri, opening_id, opening_coords)
                opening_level = ensure_level(min(point[2] for point in opening_coords))
                graph.add((opening_uri, MOOSAS.hasLevel, opening_level))
                if face_uri is not None:
                    graph.add((face_uri, MOOSAS.hasHole, opening_face_uri))

    from .exchange import capture_exchange
    capture_exchange(root, graph)
    from .energy import capture
    capture(root, graph, input_path)
    graph.serialize(destination=str(output_path), format=rdf_format)
    return graph


# Backward-compatible aliases with concise names for IO package users.
rdf_to_gbxml = convert_rdf_to_gbxml
gbxml_to_rdf = convert_gbxml_to_rdf


def is_gbxml(file_path: str | Path) -> bool:
    """Recognize gbXML documents using a generic .xml extension."""
    try:
        root = etree.parse(file_path).getroot()
    except (OSError, etree.ParseError):
        return False
    return root.tag.rsplit("}", 1)[-1].lower() == "gbxml"


def load_gbxml(file_path: str | Path):
    """Load a complete model through the BOT/MOOSAS RDF representation."""
    from ..rdf import loadRDF

    with tempfile.TemporaryDirectory(prefix=".moosas_gbxml_load_") as directory:
        rdf_path = Path(directory) / "model.ttl"
        convert_gbxml_to_rdf(file_path, rdf_path, rdf_format="turtle")
        return loadRDF(str(rdf_path), fileFormat="turtle")


def save_gbxml(model, file_path: str | Path):
    """Export model geometry, thermal evidence and exchange metadata."""
    from ..rdf import writeRDF

    with tempfile.TemporaryDirectory(prefix=".moosas_gbxml_") as directory:
        rdf_path = Path(directory) / "model.ttl"
        writeRDF(model, str(rdf_path), fileFormat="turtle")
        convert_rdf_to_gbxml(rdf_path, file_path, rdf_format="turtle")


