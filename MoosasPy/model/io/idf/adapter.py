from __future__ import annotations

import os
import copy
import math
import re
import shutil
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from eppy.modeleditor import IDF
from rdflib import Literal, URIRef
from rdflib.namespace import RDF

from ..rdf import MoosasRDF, encodeURI, decodeURI
from ..xml import loadXml
from . import IDFConversionResult, construction, input, model, parser, schedule
from .model import MoosasSettings
from .ground import apply_ground_templates, capture_ground_templates
from .materials import prune_unused_constructions_and_materials
from .version import (
    bundled_template_idf_path,
    configure_idd,
    require_idf_version,
)
from ...model import *
from ....utils import path, mixItemListToList


def _normalize_zone_name_to_space_dict(zoneNameToSpaceDict):
    """
    Normalize zoneNameToSpaceDict to {zoneName: [spaceId, ...]} and validate duplicates.

    Parameters
    ----------
    zoneNameToSpaceDict : dict or None
        Mapping where key is zone name in template IDF and value is a space id string
        or an iterable of space id strings.

    Returns
    -------
    dict
        Normalized mapping where each value is a list of unique space ids.
    """
    if zoneNameToSpaceDict is None:
        return {"": []}

    if not isinstance(zoneNameToSpaceDict, dict):
        raise TypeError("zoneNameToSpaceDict must be a dict like {'Zone_Name': 'SpaceId'}")

    normalized = {}
    usedSpaceIds = set()
    for zoneName, spaceIds in zoneNameToSpaceDict.items():
        zoneKey = str(zoneName)

        if isinstance(spaceIds, str):
            targetIds = [spaceIds]
        elif spaceIds is None:
            targetIds = []
        else:
            try:
                targetIds = list(spaceIds)
            except TypeError:
                raise TypeError(f"spaceIds for zone '{zoneKey}' must be str/list/tuple/set or None")

        # Keep stable order while removing duplicates.
        dedupedIds = []
        seen = set()
        for spId in targetIds:
            spId = str(spId)
            if spId not in seen:
                dedupedIds.append(spId)
                seen.add(spId)

        overlap = [spId for spId in dedupedIds if spId in usedSpaceIds]
        if len(overlap) > 0:
            raise ValueError(f"Duplicate spaceIds across zone mappings are not allowed: {overlap}")

        usedSpaceIds.update(dedupedIds)
        normalized[zoneKey] = dedupedIds

    if len(normalized) == 0:
        return {"": []}
    return normalized


def loadIDFTemplate(model: MoosasModel, idfTemplatePath=None, spaceIds=None, zoneName: str = "", hvac_mode: str = "ideal_loads") -> dict[str, parser.ZoneTemplate]:
    """
    Load one zone template and return independent templates keyed by space id.

    Parameters
    ----------
    model : MoosasModel
        Model containing target spaces and spaceIdDict.
    idfTemplatePath : str, optional
        Path to template IDF file. Defaults to dataBaseDir/in.idf.
    spaceIds : str or list[str], optional
        Target space ids to receive this template. If None or empty, apply to all spaces.
    zoneName : str, optional
        Zone name to select from template IDF.

    Returns
    -------
    dict[str, parser.ZoneTemplate]
        Zone-specific templates keyed by target space id.

    """
    configure_idd()
    idfTemplatePath = require_idf_version(idfTemplatePath or bundled_template_idf_path())

    idf = IDF(idfTemplatePath)
    if hvac_mode not in {"simple", "ideal_loads"}:
        raise ValueError("hvac_mode must be 'simple' or 'ideal_loads'")
    zTemplate: parser.ZoneTemplate = parser.ZoneTemplate.fromIDF(
        idf,
        zoneName=zoneName,
        hvac_mode=hvac_mode,
    )
    if zTemplate.isEmpty():
        print(f"\n******Warning: no valid zone template was found for Name='{zoneName}'")
        return {}

    if isinstance(spaceIds, str):
        spaceIds = [spaceIds]

    if spaceIds:
        missingSpaceIds = [spId for spId in spaceIds if spId not in model.spaceIdDict]
        if len(missingSpaceIds) > 0:
            raise ValueError(f"Invalid spaceIds: {missingSpaceIds}")
        targetSpaces = [model.spaceIdDict[spId] for spId in spaceIds]
    else:
        targetSpaces = list(model.spaceList)
        
    templates = {}
    for si, space in enumerate(targetSpaces):
        print(f"\rIDF: overwriting zonal settings: {si + 1}/{len(targetSpaces)}=>{space.id}", end='')
        templates[str(space.id)] = copy.deepcopy(zTemplate).appliedToZone(space)
        if space.is_open():
            print(f'\n******Warring: EnergyPlus do not support void space: {space.id}')
    return templates


def _idf_auto_zone_mapping(model: MoosasModel, idf_template_path: str) -> dict[str, list[str]]:
    """Map detected attic and basement spaces to matching template Zones."""
    try:
        idf = IDF(idf_template_path)
        names = [str(zone['Name']).strip() for zone in idf.idfobjects['ZONE'] if str(zone['Name']).strip()]
    except Exception:
        return {"": []}
    attic_zone = next((name for name in names if re.search(r'\battic\b', name, re.I)), "")
    basement_zone = next((name for name in names if re.search(r'\bbasement\b', name, re.I)), "")
    room_zone = next((name for name in names if name not in {attic_zone, basement_zone}), basement_zone or attic_zone)
    mapping = defaultdict(list)
    for space in model.spaceList:
        space_type = getattr(space, 'space_type', 'room')
        if space_type == 'attic' and attic_zone:
            zone_name = attic_zone
        elif space_type == 'basement' and basement_zone:
            zone_name = basement_zone
        else:
            zone_name = room_zone
        mapping[zone_name].append(str(space.id))
    return _normalize_zone_name_to_space_dict(dict(mapping))
def _idf_apply_attic_mixing(idf, model, template_path):
    """Copy reference-attic ZoneMixing objects to each geometrically adjacent room."""
    try:
        reference = IDF(template_path)
    except Exception:
        return
    attic_templates = {str(z['Name']).strip() for z in reference.idfobjects['ZONE'] if re.search(r'\battic\b', str(z['Name']), re.I)}
    if not attic_templates:
        return
    refs = [obj for obj in reference.idfobjects['ZONEMIXING'] if str(obj['Zone_or_Space_Name']).strip() in attic_templates]
    for attic in [s for s in model.spaceList if getattr(s, 'space_type', 'room') == 'attic']:
        sources = sorted({sid for face in attic.floor.face for sid in face.space if sid != attic.id and sid in model.spaceIdDict})
        if not sources:
            continue
        for ref_index, ref in enumerate(refs, start=1):
            for index, source in enumerate(sources):
                obj = idf.newidfobject('ZoneMixing')
                for field in ref.fieldnames:
                    if field != 'key':
                        obj[field] = ref[field]
                obj['Name'] = f'{attic.id}_mix_{ref_index}_{index + 1}'
                obj['Zone_or_Space_Name'] = attic.id
                obj['Source_Zone_or_Space_Name'] = source
                if str(obj['Design_Flow_Rate_Calculation_Method']).strip().lower() == 'flow/zone':
                    try: obj['Design_Flow_Rate'] = float(ref['Design_Flow_Rate']) / len(sources)
                    except (TypeError, ValueError): pass
def _copy_idf_schedule_files(template_path, output_path):
    """Copy relative Schedule:File resources next to an exported IDF."""
    try:
        reference = IDF(template_path)
    except Exception:
        return
    template_dir = Path(template_path).parent
    output_dir = Path(output_path).parent
    for schedule in reference.idfobjects['SCHEDULE:FILE']:
        filename = str(schedule['File_Name']).strip()
        if not filename or os.path.isabs(filename):
            continue
        source = template_dir / filename
        destination = output_dir / filename
        if source.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)


def _idf_hvac_object_types(idf):
    """Return template object classes that belong to an explicit HVAC graph."""
    prefixes = (
        'AIRFLOWNETWORK', 'AIRLOOPHVAC', 'AIRTERMINAL', 'AVAILABILITYMANAGER',
        'BOILER', 'BRANCH', 'CHILLER', 'COIL', 'CONNECTOR', 'CONTROLLER',
        'COOLINGTOWER', 'DUCT:', 'EVAPORATIVECOOLER', 'FAN', 'HEATEXCHANGER',
        'HVACTEMPLATE', 'MIXER', 'NODELIST', 'OUTDOORAIR:', 'PLANTLOOP',
        'ENERGYMANAGEMENTSYSTEM',
        'PLANTEQUIPMENT', 'PUMP', 'SETPOINTMANAGER', 'SPLITTER', 'THERMOSTAT',
        'ZONEHVAC', 'SPACEHVAC', 'SIZING:SYSTEM',
    )
    return [
        object_type
        for object_type in idf.idfobjects
        if object_type.upper().startswith(prefixes)
    ]


def _writeIDF_default(model: MoosasModel, outputPath: str, idfTemplatePath=None, zoneNameToSpaceDict=None, hvac_mode: str = "ideal_loads"):
    """
    Write an EnergyPlus Input Data File (IDF) based on a MoosasModel.

    Parameters
    ----------
    model : MoosasModel
        A model instance containing building geometry and settings to be converted into IDF format.
        Must provide methods `getAllFaces`, `spaceIdDict`, and `spaceList`, and associated attributes
        for space and surface properties.
    outputPath : str
        Path to save the generated IDF file. The directory must be writable.
    idfTemplatePath : str, optional
        Path to template IDF file. Defaults to dataBaseDir/in.idf.
    zoneNameToSpaceDict : dict, optional
        Mapping from template zone name to target space ids.
        Example: {"Zone_A": "Space-1", "Zone_B": ["Space-2", "Space-3"]}
        If None, equivalent to {"": all spaces}.

    Returns
    -------
    None
        This function does not return any value. It writes the IDF file to the specified path and prints progress information.
    """
    print('IDF: initialization from IDF file...')
    configure_idd()
    idfTemplatePath = require_idf_version(idfTemplatePath or bundled_template_idf_path())
    moElements = model.getAllFaces(dumpUseless=True)

    if zoneNameToSpaceDict is None and idfTemplatePath:
        zoneNameToSpaceDict = _idf_auto_zone_mapping(model, idfTemplatePath)
    zoneMap = _normalize_zone_name_to_space_dict(zoneNameToSpaceDict)
    idfSpaces = [space for space in model.spaceList if not space.is_open()]
    idfSpaceById = {str(space.id): space for space in idfSpaces}
    validSpaceIds = set(idfSpaceById)
    zoneMap = {
        zoneName: (
            list(idfSpaceById)
            if not spaceIds
            else [spaceId for spaceId in spaceIds if spaceId in validSpaceIds]
        )
        for zoneName, spaceIds in zoneMap.items()
    }
    zoneMap = {zoneName: spaceIds for zoneName, spaceIds in zoneMap.items() if spaceIds}
    zTemplate: parser.ZoneTemplate = None
    templatesBySpaceId: dict[str, parser.ZoneTemplate] = {}
    objectHints = set()
    assignedSpaceIds = set()
    allSpaceIds = list(idfSpaceById)

    # Apply templates by zone->space mapping. When space list is empty, it means all spaces.
    for zoneName, targetSpaceIds in zoneMap.items():
        mappedSpaceIds = list(targetSpaceIds)
        thisTemplates = loadIDFTemplate(
            model,
            idfTemplatePath=idfTemplatePath,
            spaceIds=mappedSpaceIds,
            zoneName=zoneName,
            hvac_mode=hvac_mode,
        )
        if not thisTemplates:
            print(f"\n******Warning: skip empty template for zone '{zoneName}'")
            continue

        templatesBySpaceId.update(thisTemplates)
        thisTemplate = next(iter(thisTemplates.values()))
        if zTemplate is None:
            zTemplate = thisTemplate
        for template in thisTemplates.values():
            objectHints.update(template.objectList.keys())
        assignedSpaceIds.update(mappedSpaceIds)

    if zTemplate is None:
        raise ValueError("No valid ZoneTemplate was loaded for zoneNameToSpaceDict")

    unassignedSpaceIds = [spId for spId in allSpaceIds if spId not in assignedSpaceIds]
    if unassignedSpaceIds:
        raise ValueError(f"No IDF template was assigned to spaces: {unassignedSpaceIds}")

    # remote existing zone-related objects
    removeHint = list(objectHints) + [
        'ZoneInfiltration:DesignFlowRate',
        'ZoneInfiltration:EffectiveLeakageArea',
        'ZoneVentilation:DesignFlowRate',
        'ZoneVentilation:WindandStackOpenArea',
        'OtherEquipment',
        'ElectricEquipment',
        'People',
        'Lights',
        'Sizing:Zone',
        'DesignSpecification:OutdoorAir',
        'DesignSpecification:ZoneAirDistribution',
        'ZoneControl:Thermostat',
        'ThermostatSetpoint:DualSetpoint',
        'ZoneHVAC:EquipmentConnections',
        'ZoneHVAC:EquipmentList',
        'ZoneHVAC:IdealLoadsAirSystem',
        'NodeList',
        'Zone', 'WaterUse:Equipment', 'WaterUse:Connections', 'WaterHeater:Mixed',
        'WaterHeater:Sizing', 'BuildingSurface:Detailed',
                           'FenestrationSurface:Detailed', 'Shading:Building:Detailed',
                           'Shading:Zone:Detailed', 'Shading:Site:Detailed', 'InternalMass',
                                       'SurfaceProperty:ExposedFoundationPerimeter',
                                       'SurfaceProperty:IncidentSolarMultiplier',
                                       'Space', 'SpaceList', 'ZoneMixing', 'Construction:AirBoundary',
                                       'DesignSpecification:OutdoorAir:SpaceList']
    # These template-level objects refer to the template's zones or air loops.
    # They cannot be retained after ideal-loads export replaces those objects.
    removeHint.append('ZoneList')
    idf = zTemplate.idf
    groundTemplates = capture_ground_templates(idf, zoneMap)
    if hvac_mode == "ideal_loads":
        removeHint.extend(_idf_hvac_object_types(idf))
    for h in removeHint:
        idf.idfobjects[h] = []
        print(f"\rIDF: cleaning existing objects: {h}", end='')
    print()

    geometry_rules = idf.idfobjects['GLOBALGEOMETRYRULES']
    if not geometry_rules:
        geometry_rules = [idf.newidfobject(
            'GlobalGeometryRules',
            Starting_Vertex_Position='UpperLeftCorner',
            Coordinate_System='Relative',
        )]
    for rules in geometry_rules:
        rules.Vertex_Entry_Direction = 'Counterclockwise'

    # add moosas defines objects
    # get type limits
    typeLimitsName = [idfobj['Name'] for idfobj in idf.idfobjects['ScheduleTypeLimits']]
    for typeLimit in schedule.typeLimitSettings:
        if typeLimit.params['Name'] not in typeLimitsName:
            typeLimit.applyToIDF(idf)
    airboundary = MoosasSettings(construction.airBoundaryDefault)
    airboundary.applyToIDF(idf)

    # encoding geometries
    for wi, wall in enumerate(moElements['MoosasWall']):
        try:
            spaceId = next((str(spaceId) for spaceId in wall.space if str(spaceId) in idfSpaceById), None)
            if spaceId is not None:
                print(f"\rIDF: encoding walls: {wi+1}/{len(moElements['MoosasWall'])}", end='')
                space = idfSpaceById[spaceId]
                # Element settings take precedence over the derived zone averages.
                wallU = wall.settings.get('u_value', space.settings['zone_wallU'])
                glazing = wall.glazingElement[0] if wall.glazingElement else None
                winU = glazing.settings.get('u_value', space.settings['zone_winU']) if glazing else space.settings['zone_winU']
                SHGC = glazing.settings.get('shgc', space.settings['zone_win_SHGC']) if glazing else space.settings['zone_win_SHGC']
                wallConstruction = zTemplate.getConstruction('opaque', wallU)
                windowConstruction = zTemplate.getConstruction('window', winU, SHGC)
                if wall.category == 2:
                    input.createThermalSurface(idf, wall, 'Wall', "Moosas Air Boundary",
                                                     None,encodeWindow=False)
                else:
                    input.createThermalSurface(idf, wall, 'Wall', wallConstruction.params['Name'],
                                                     windowConstruction.params['Name'])
        except IndexError as e:
            print(f"\n  Warning: Wall {wi} (UID: {wall.Uid}) encoding failed - list index error with spaces: {wall.space} - {str(e)}")
        except KeyError as e:
            missing_id = str(e).strip("'")
            print(f"\n  Warning: Wall {wi} (UID: {wall.Uid}) encoding failed - space ID not found: {missing_id} (available: {wall.space})")
        except Exception as e:
            print(f"\n  Warning: Wall {wi} (UID: {wall.Uid}) encoding failed - {type(e).__name__}: {str(e)}")
    print()
    for fi, face in enumerate(moElements['MoosasFace']):
        try:
            spaceId = next((str(spaceId) for spaceId in face.space if str(spaceId) in idfSpaceById), None)
            if spaceId is not None:
                print(f"\rIDF: encoding faces: {fi+1}/{len(moElements['MoosasFace'])}", end='')
                faceType = 'Floor'
                space = idfSpaceById[spaceId]
                if space.ceiling and face in space.ceiling.face:
                    faceType = 'Roof' if len(face.space) == 1 else 'Ceiling'
                wallU = face.settings.get('u_value', space.settings['zone_wallU'])
                glazing = face.glazingElement[0] if face.glazingElement else None
                winU = glazing.settings.get('u_value', space.settings['zone_winU']) if glazing else space.settings['zone_winU']
                SHGC = glazing.settings.get('shgc', space.settings['zone_win_SHGC']) if glazing else space.settings['zone_win_SHGC']
                wallConstruction = zTemplate.getConstruction('opaque', wallU)
                windowConstruction = zTemplate.getConstruction('window', winU, SHGC)
                if face.category == 2:
                    input.createThermalSurface(idf, face, faceType, "Moosas Air Boundary",
                                                     None,encodeWindow=False)
                else:
                    input.createThermalSurface(idf, face, faceType, wallConstruction.params['Name'],
                                                     windowConstruction.params['Name'])
        except IndexError as e:
            print(f"\n  Warning: Face {fi} encoding failed - invalid space list: {face.space} - {e}")
        except KeyError as e:
            print(f"\n  Warning: Face {fi} encoding failed - space not found: {face.space[0] if len(face.space) > 0 else 'empty'} - {e}")
        except Exception as e:
            print(f"\n  Warning: Face {fi} encoding failed - {e}")
    print()

    for shading in model.shadingList:
        geometries = list(shading.geometry)
        for index, geometry in enumerate(geometries, start=1):
            name = str(shading.Uid) if len(geometries) == 1 else f'{shading.Uid}-{index}'
            shading_settings = MoosasSettings({
                'key': 'Shading:Building:Detailed',
                'Name': name,
            })
            input.encodeFace(shading_settings, geometry.face, geometry.normal)
            shading_settings.applyToIDF(idf)

    # writing zonal settings
    for si, space in enumerate(idfSpaces):
        print(f"\rIDF: encoding zones: {si+1}/{len(idfSpaces)}", end='')
        templatesBySpaceId[str(space.id)].applyToIDF(idf)

    if idfTemplatePath:
        _idf_apply_attic_mixing(idf, model, idfTemplatePath)
    apply_ground_templates(idf, groundTemplates)
    prune_unused_constructions_and_materials(idf)
    idf.save(outputPath)
    if idfTemplatePath:
        _copy_idf_schedule_files(idfTemplatePath, outputPath)
    print()
    return zoneMap, templatesBySpaceId


def exportIDF(model: MoosasModel, outputPath: str, idfTemplatePath=None, zoneNameToSpaceDict=None, hvac_mode: str = "ideal_loads"):
    """Write an IDF file without constructing the optional IDF RDF alignment graph.

    ``hvac_mode='ideal_loads'`` removes the template HVAC graph and creates
    one Ideal Loads system per conditioned target zone. ``hvac_mode='simple'``
    preserves the currently supported single Zone HVAC equipment migration.
    """
    from ....transform.alignment import default_template_idf_path

    resolved_template = default_template_idf_path(idfTemplatePath)
    _writeIDF_default(
        model,
        outputPath,
        idfTemplatePath=resolved_template,
        zoneNameToSpaceDict=zoneNameToSpaceDict,
        hvac_mode=hvac_mode,
    )


def writeIDF(model: MoosasModel, outputPath: str, idfTemplatePath=None, zoneNameToSpaceDict=None, hvac_mode: str = "ideal_loads") -> IDFConversionResult:
    """Write IDF and return its IDF-specific conversion state.

    The default ``ideal_loads`` mode is intended for heterogeneous templates;
    use ``simple`` only when the template's Zone HVAC equipment is supported
    by the explicit simple migration path.
    """
    from ....transform.alignment import IDFtoOWL, default_template_idf_path, link_idf_graph_to_moosas

    resolved_template = default_template_idf_path(idfTemplatePath)
    zone_map, templates = _writeIDF_default(
        model,
        outputPath,
        idfTemplatePath=resolved_template,
        zoneNameToSpaceDict=zoneNameToSpaceDict,
        hvac_mode=hvac_mode,
    )
    generated_graph = IDFtoOWL(outputPath)
    linked_graph, uri_map = link_idf_graph_to_moosas(generated_graph, model)
    return IDFConversionResult(
        model=model,
        zone_to_space_ids=zone_map,
        templates_by_space_id=templates,
        graph=linked_graph,
        graph_source=os.path.abspath(outputPath),
        uri_map=uri_map,
    )


def find_closest_field(field_list: list, target_field: str) -> str:
    """
    从字符串列表中找到与目标字段最接近的匹配项（结合正则和相似度）。
    :param field_list: 待匹配的字符串列表。
    :param target_field: 目标字段名称
    :return: 最接近的匹配项（无匹配时返回空字符串）
    """
    # 步骤1：正则预处理（模糊匹配核心关键词，提升候选准确性）
    # 转义目标字段中的正则元字符，避免语法错误
    escaped_target = re.escape(target_field)
    # 构建模糊匹配正则：允许目标字段前后有其他字符，且忽略大小写。
    pattern = re.compile(f".*{escaped_target}.*", re.IGNORECASE)

    # 步骤2：筛选候选项（至少包含目标字段核心字符的项）
    candidates = [field for field in field_list if pattern.match(str(field))]

    # 若无正则匹配的候选项，直接基于全列表计算相似度。
    if not candidates:
        candidates = field_list

    # 步骤3：计算候选项与目标字段的相似度（编辑距离），排序
    def similarity(a: str, b: str) -> float:
        """计算两个字符串的相似度（0-1，1 为完全一致）。"""
        return SequenceMatcher(None, str(a).strip(), str(b).strip()).ratio()

    # 按相似度降序排序，相似度相同则按字符串长度接近度排序
    sorted_candidates = sorted(
        candidates,
        key=lambda x: (
            similarity(x, target_field),  # 优先按相似度
            -abs(len(str(x)) - len(target_field))  # 次优先按长度接近度。
        ),
        reverse=True
    )

    # 步骤 4：返回最接近的项（无列表元素时返回空字符串）。
    return sorted_candidates[0] if sorted_candidates else ""


def IDFtoOWL(idfTemplatePath):
    """
    Translate an IDF (Input Data File) knowledge base into an OWL (Web Ontology Language) RDF graph.

        Parameters
        ----------
        idfTemplatePath : str
            Path to the IDF template file to be converted. The file contains building energy model input data
            structured according to EnergyPlus Input/Output Reference definitions.

        Returns
        -------
        Graph
            An RDFlib Graph object representing the IDF data as an OWL ontology. The graph includes classes,
            properties, and instances derived from the IDF file, with semantics aligned to the EnergyPlus
            InputOutputReference documentation. Subjects are defined under the 'idf' namespace.
    """
    configure_idd()
    idfTemplatePath = require_idf_version(idfTemplatePath)
    rootFile = IDF(idfTemplatePath)
    rootGraph = MoosasRDF()

    def encodedObject(objectName, className, obj, objectType):
        if "memo" in obj.objidd[0]:
            memo = ' '.join(obj.objidd[0]['memo'])
        else:
            memo = "This class has no comment"
        ent = rootGraph.encode_entity(name=">".join([className, objectName]),
                                      label=" ".join([className, objectName]),
                                      entityType=objectType,
                                      description=memo)
        rootGraph.add((ent, rootGraph.idf.instanceOf, encodeURI(className)))

        # embedding field and field value
        for idx, fieldIdd in enumerate(obj.objidd[1:len(obj.obj)]):
            # fieldName = fieldIdd['field'][0]
            fieldName = obj.objls[idx + 1]
            description = f'{fieldName} instance for the object {objectName} in class {className}'
            fieldEnt = rootGraph.encode_entity(name=">".join([className, objectName, fieldName]),
                                               label=" ".join([className, objectName, fieldName]),
                                               entityType=rootGraph.idf.fieldInstance,
                                               description=description)
            rootGraph.add((ent, rootGraph.idf.hasField, fieldEnt))
            rootGraph.add((fieldEnt, rootGraph.idf.instanceOf, encodeURI(fieldName)))
            fieldValue = obj.obj[idx + 1]
            if fieldValue != '':
                if "type" in fieldIdd:
                    if fieldIdd['type'][0] == 'object-list':
                        rootGraph.add((fieldEnt, rootGraph.idf.hasValue, encodeURI(fieldValue)))
                    else:
                        rootGraph.add((fieldEnt, rootGraph.idf.hasValue, Literal(fieldValue)))
                else:
                    rootGraph.add((fieldEnt, rootGraph.idf.hasValue, Literal(fieldValue)))

    for objHint in rootFile.idfobjects.keys():
        # serialized Processing idf class
        if len(rootFile.idfobjects[objHint]) > 0:

            # serialized Processing idf object
            for obj in rootFile.idfobjects[objHint]:

                # encoding normal objects
                if len(obj.obj) >= 2 and re.search('name', str(obj.objidd[1]['field']), re.IGNORECASE) is not None:
                    encodedObject(obj.obj[1], objHint, obj, rootGraph.idf.idfObject)

                # encoding output variables
                elif objHint == 'OUTPUT:VARIABLE':
                    encodedObject(obj.obj[2], objHint, obj, encodeURI(objHint))

                # encoding unique object
                else:
                    encodedObject(objHint + '_instance', objHint, obj, rootGraph.idf.idfUniqueObject)

    return rootGraph


def OWLtoIDF(owl, outFile):
    """
    Convert an OWL ontology graph to an IDF (Input Data File) format used by EnergyPlus.

    Parameters
    ----------
    owl : Graph or str
        An RDFlib Graph object containing the OWL ontology data, or a string path to an OWL file.
    outFile : str
        Path to the output file where the generated IDF will be saved.

    Returns
    -------
    IDF
        An IDF object representing the EnergyPlus input data file, populated with objects
        derived from the input OWL graph and saved to the specified output path.
    """
    # copy the graph into a MoosasRDF
    if isinstance(owl, str):
        newowl = MoosasRDF()
        newowl.parse(owl)
        owl = newowl
    graph = MoosasRDF()

    # ensure the owl is an edGraph() by copying all triples to a new graph
    for triple in owl:
        graph.add(triple)

    configure_idd()
    idfFile = IDF(require_idf_version(bundled_template_idf_path()))
    for key in idfFile.idfobjects:
        idfFile.idfobjects[key] = []

    def decodeObject(objectURI):
        objHint = graph.getObject(objectURI, graph.idf.instanceOf)[0]
        obj = idfFile.newidfobject(decodeURI(objHint))
        for fieldURI in graph.getObject(objectURI, graph.idf.hasField):
            fieldName = decodeURI(graph.getObject(fieldURI, graph.idf.instanceOf)[0])
            fieldName = re.sub(' ', '_', fieldName)

            # try to match field
            if fieldName not in obj.objls:
                print(f"***Warning: {fieldName} not found in {decodeURI(objHint)}")
                fieldName = find_closest_field(obj.objls, fieldName)

            if fieldName != '':
                fieldValue = graph.getObject(fieldURI, graph.idf.hasValue)
                if fieldValue:
                    if isinstance(fieldValue, URIRef):
                        fieldValue = decodeURI(fieldValue)
                    obj[fieldName] = str(fieldValue)

        return obj

    # add unique objects
    for idfObject in graph.subjects(RDF.type, graph.idf.idfUniqueObject):
        decodeObject(idfObject)

    # add normal objects
    for idfObject in graph.subjects(RDF.type, graph.idf.idfObject):
        decodeObject(idfObject)

    # add output objects
    for outputObject in graph.subjects(RDF.type, encodeURI("OUTPUT:VARIABLE")):
        decodeObject(outputObject)

    idfFile.save(outFile)
    require_idf_version(outFile)
    return idfFile


def getIdfObjects(graph: MoosasRDF, objectURI: URIRef) -> MoosasRDF:
    """
    Given an input graph and an object URI, find all objects derived from the input idfObjectURI.
    Parameters
    ----------
    graph : MoosasRDF
        A MoosasRDF containing the OWL ontology data from IDFtoOWL().
    objectURI: URIRef
        An RDFlib URI which targets to a idfObject of the input graph.

    Returns
    -------
    subGraph : MoosasRDF
        An subGraph containing the zone settings.
    """
    for typeURI in graph.objects(objectURI, graph.rdf.type):
        if typeURI != graph.idf.idfObject:
            return MoosasRDF()
    subGraph = MoosasRDF()
    for s, p, o in graph.triples((objectURI, None, None)):
        subGraph.add((s, p, o))
    for field in graph.objects(objectURI, graph.idf.hasField):
        for s, p, o in graph.triples((field, None, None)):
            subGraph.add((s, p, o))
    return subGraph


def extractZoneTemplate(graph: MoosasRDF):
    """
    Extract the first zone and all related settings from a MoosasRDF object.
    except for the geometry information.
    Parameters
    ----------
    graph : MoosasRDF
        A MoosasRDF containing the OWL ontology data from IDFtoOWL().


    Returns
    -------
    subGraph : MoosasRDF
        An subGraph containing the zone settings.
    """
    subGraph = MoosasRDF()
    zoneURI = mixItemListToList(graph.getSubject(graph.idf.instanceOf, encodeURI("ZONE")))[0]
    zoneName = decodeURI(zoneURI).split(">")[1]
    print(zoneName)
    relateObjects = set()

    def _checkAddObj(objURI):
        idfClass = decodeURI(graph.getObject(objURI, graph.idf.instanceOf))
        if idfClass is not None:
            if idfClass == "BUILDINGSURFACE:DETAILED" or idfClass == "FENESTRATIONSURFACE:DETAILED":
                return False
            else:
                relateObjects.add(objURI)
                return True

    # find object's name contain the zone name
    for obj in graph.subjects(RDF.type, graph.idf.idfObject):
        label = str(graph.getObject(obj, graph.rdfs.label))
        if re.search(zoneName, label) is not None:
            _checkAddObj(obj)

    # find field values contain the zone name, as object-list; then add the object has this field
    for fieldURI in graph.subjects(RDF.type, graph.idf.fieldInstance):
        for fieldValue in graph.objects(fieldURI, graph.idf.hasValue):
            if isinstance(fieldValue, URIRef):
                fieldValue = decodeURI(fieldValue)
                if re.search(zoneName, fieldValue) is not None:
                    theObject = graph.getSubject(graph.idf.hasField, fieldURI)
                    if theObject is not None:
                        _checkAddObj(theObject)

    for validObj in relateObjects:
        print(decodeURI(validObj))
        objGraph = getIdfObjects(graph, validObj)
        subGraph += objGraph

    return subGraph


IDF_TO_GEO_PRECISION = 6


@dataclass
class _IDFSurfaceRecord:
    name: str
    family: str
    surface_type: str
    zone_name: str
    boundary_condition: str
    boundary_object: str
    parent_surface: str
    construction_name: str
    vertices: list[tuple[float, float, float]]


@dataclass
class _IDFUnifiedFace:
    key: str
    geo_id: str
    uid: str
    cat: int
    vertices: list[tuple[float, float, float]]
    normal: tuple[float, float, float]
    records: list[_IDFSurfaceRecord]

    @property
    def level(self) -> float:
        return min(v[2] for v in self.vertices)

    @property
    def offset(self) -> float:
        return 0.0

    @property
    def is_horizontal(self) -> bool:
        return abs(self.normal[2]) >= 0.9


def _idf_default_output_path(idfPath: str, extension: str) -> str:
    base, _ = os.path.splitext(idfPath)
    return base + extension


def _idf_as_float(value) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _idf_round_xyz(point: tuple[float, float, float], precision: int = IDF_TO_GEO_PRECISION) -> tuple[float, float, float]:
    return (
        round(float(point[0]), precision),
        round(float(point[1]), precision),
        round(float(point[2]), precision),
    )


def _idf_poly_normal(vertices: list[tuple[float, float, float]]) -> tuple[float, float, float]:
    if len(vertices) < 3:
        return 0.0, 0.0, 1.0
    p1, p2, p3 = vertices[0], vertices[1], vertices[2]
    ux, uy, uz = p2[0] - p1[0], p2[1] - p1[1], p2[2] - p1[2]
    vx, vy, vz = p3[0] - p1[0], p3[1] - p1[1], p3[2] - p1[2]
    nx = uy * vz - uz * vy
    ny = uz * vx - ux * vz
    nz = ux * vy - uy * vx
    norm = math.sqrt(nx * nx + ny * ny + nz * nz)
    if norm == 0:
        return 0.0, 0.0, 1.0
    return nx / norm, ny / norm, nz / norm


def _idf_canonical_signature(vertices: list[tuple[float, float, float]], precision: int = IDF_TO_GEO_PRECISION) -> str:
    rounded = [_idf_round_xyz(v, precision) for v in vertices]
    seq = sorted(rounded)
    return "|".join(f"{x:.{precision}f},{y:.{precision}f},{z:.{precision}f}" for x, y, z in seq)


def _idf_get_vertex_triplets(idf_obj, vertex_count: int) -> list[tuple[float, float, float]]:
    points = []
    for i in range(1, vertex_count + 1):
        x = _idf_as_float(idf_obj[f"Vertex_{i}_Xcoordinate"])
        y = _idf_as_float(idf_obj[f"Vertex_{i}_Ycoordinate"])
        z = _idf_as_float(idf_obj[f"Vertex_{i}_Zcoordinate"])
        points.append(_idf_round_xyz((x, y, z)))
    return points


def _idf_safe_obj_value(idf_obj, field: str, default: str = "") -> str:
    try:
        value = idf_obj[field]
    except Exception:
        return default
    if value is None:
        return default
    return str(value)


def _idf_map_cat(rec: _IDFSurfaceRecord, is_horizontal: bool) -> int:
    st = (rec.surface_type or "").upper()
    bc = (rec.boundary_condition or "").upper()
    cn = (rec.construction_name or "").upper()

    if rec.family == "shading":
        return -1

    if rec.family == "fenestration":
        if st in {"SKYLIGHT", "TUBULARDAYLIGHTDOME", "TUBULARDAYLIGHTDIFFUSER"} or is_horizontal:
            return 6
        # Interior windows are represented by paired fenestration objects linked by
        # Outside_Boundary_Condition_Object in EnergyPlus.
        if str(rec.boundary_object).strip() != "":
            return 1
        if "TRANSLUC" in cn:
            return 1
        return 5

    if rec.family == "building":
        if st in {"FLOOR", "ROOF", "CEILING"} or is_horizontal:
            return 4
        if "AIR" in cn and "BOUNDARY" in cn:
            return 2
        if st == "WALL":
            return 3
        if bc in {"ADIABATIC", "GROUND", "OUTDOORS", "SURFACE", "ZONE"}:
            return 0
        return 0

    return -2


def _idf_read_surfaces(idfPath: str) -> list[_IDFSurfaceRecord]:
    configure_idd()
    idf = IDF(require_idf_version(idfPath))
    records: list[_IDFSurfaceRecord] = []
    building_zone_by_name: dict[str, str] = {}

    for obj in idf.idfobjects["BUILDINGSURFACE:DETAILED"]:
        n = int(_idf_as_float(obj.Number_of_Vertices))
        if n < 3:
            continue
        obj_name = _idf_safe_obj_value(obj, "Name")
        obj_zone = _idf_safe_obj_value(obj, "Zone_Name")
        if obj_name:
            building_zone_by_name[obj_name] = obj_zone
        records.append(
            _IDFSurfaceRecord(
                name=obj_name,
                family="building",
                surface_type=_idf_safe_obj_value(obj, "Surface_Type"),
                zone_name=obj_zone,
                boundary_condition=_idf_safe_obj_value(obj, "Outside_Boundary_Condition"),
                boundary_object=_idf_safe_obj_value(obj, "Outside_Boundary_Condition_Object"),
                parent_surface="",
                construction_name=_idf_safe_obj_value(obj, "Construction_Name"),
                vertices=_idf_get_vertex_triplets(obj, n),
            )
        )

    for obj in idf.idfobjects["FENESTRATIONSURFACE:DETAILED"]:
        n = int(_idf_as_float(obj.Number_of_Vertices))
        if n < 3:
            continue
        parent_surface_name = _idf_safe_obj_value(obj, "Building_Surface_Name")
        zone_name = _idf_safe_obj_value(obj, "Zone_Name", default="")
        if not zone_name and parent_surface_name:
            zone_name = building_zone_by_name.get(parent_surface_name, "")
        records.append(
            _IDFSurfaceRecord(
                name=_idf_safe_obj_value(obj, "Name"),
                family="fenestration",
                surface_type=_idf_safe_obj_value(obj, "Surface_Type"),
                zone_name=zone_name,
                boundary_condition=_idf_safe_obj_value(obj, "Outside_Boundary_Condition"),
                boundary_object=_idf_safe_obj_value(obj, "Outside_Boundary_Condition_Object"),
                parent_surface=parent_surface_name,
                construction_name=_idf_safe_obj_value(obj, "Construction_Name"),
                vertices=_idf_get_vertex_triplets(obj, n),
            )
        )

    shading_classes = [
        "SHADING:BUILDING:DETAILED",
        "SHADING:ZONE:DETAILED",
        "SHADING:SITE:DETAILED",
    ]
    for cls in shading_classes:
        for obj in idf.idfobjects.get(cls, []):
            n = int(_idf_as_float(getattr(obj, "Number_of_Vertices", 0)))
            if n < 3:
                continue
            records.append(
                _IDFSurfaceRecord(
                    name=str(getattr(obj, "Name", f"{cls}_unnamed")),
                    family="shading",
                    surface_type=cls,
                    zone_name=str(getattr(obj, "Zone_Name", "")),
                    boundary_condition="",
                    boundary_object="",
                    parent_surface="",
                    construction_name="",
                    vertices=_idf_get_vertex_triplets(obj, n),
                )
            )

    return records


def _idf_unify_faces(records: list[_IDFSurfaceRecord]) -> tuple[list[_IDFUnifiedFace], dict[str, _IDFUnifiedFace], dict[str, int]]:
    rec_by_name = {r.name: r for r in records}
    assigned: dict[str, str] = {}
    pairs: list[tuple[str, str]] = []
    stats = {"paired": 0, "fallback": 0}

    for rec in records:
        if rec.name in assigned:
            continue
        if rec.boundary_condition.upper() != "SURFACE" or not rec.boundary_object:
            continue
        other = rec_by_name.get(rec.boundary_object)
        if other is None:
            continue
        if other.boundary_condition.upper() != "SURFACE" or other.boundary_object != rec.name:
            continue
        if other.family != rec.family:
            continue
        a, b = sorted((rec.name, other.name))
        key = f"pair::{a}::{b}"
        if a in assigned or b in assigned:
            continue
        assigned[a] = key
        assigned[b] = key
        pairs.append((a, b))
        stats["paired"] += 1

    grouped: dict[str, list[_IDFSurfaceRecord]] = defaultdict(list)
    for rec in records:
        if rec.name in assigned:
            continue
        sig = _idf_canonical_signature(rec.vertices)
        gkey = f"geom::{rec.family}::{sig}"
        grouped[gkey].append(rec)

    unified: list[_IDFUnifiedFace] = []
    rec_to_face: dict[str, _IDFUnifiedFace] = {}
    next_id = 1

    def add_unified(key: str, rec_list: list[_IDFSurfaceRecord]) -> None:
        nonlocal next_id
        vertices = rec_list[0].vertices
        normal = _idf_poly_normal(vertices)
        cat = _idf_map_cat(rec_list[0], abs(normal[2]) >= 0.9)
        geo_id = f"n{next_id}"
        if cat == 3:
            uid = f"wall_{next_id}"
        elif cat == 4:
            uid = f"face_{next_id}"
        elif cat == 5:
            uid = f"gls_{next_id}"
        elif cat == 6:
            uid = f"sky_{next_id}"
        elif cat == -1:
            uid = f"shd_{next_id}"
        else:
            uid = f"surf_{next_id}"
        face = _IDFUnifiedFace(key=key, geo_id=geo_id, uid=uid, cat=cat, vertices=vertices, normal=normal, records=rec_list)
        unified.append(face)
        for r in rec_list:
            rec_to_face[r.name] = face
        next_id += 1

    for a, b in pairs:
        add_unified(assigned[a], [rec_by_name[a], rec_by_name[b]])

    for gkey, rec_list in grouped.items():
        for rec in rec_list:
            assigned[rec.name] = gkey
        add_unified(gkey, rec_list)
        stats["fallback"] += 1

    return unified, rec_to_face, stats


def _idf_point2d(p: tuple[float, float, float], precision: int = 3) -> tuple[float, float]:
    return round(float(p[0]), precision), round(float(p[1]), precision)


def _idf_segment_key(a: tuple[float, float], b: tuple[float, float]) -> tuple[tuple[float, float], tuple[float, float]]:
    return (a, b) if a <= b else (b, a)


def _idf_face_bottom_segment(face: _IDFUnifiedFace, precision: int = 3) -> tuple[tuple[float, float], tuple[float, float]] | None:
    min_z = min(v[2] for v in face.vertices)
    bot_pts = [_idf_point2d(v, precision) for v in face.vertices if abs(v[2] - min_z) <= 1e-6]
    ordered = []
    for p in bot_pts:
        if not ordered or ordered[-1] != p:
            ordered.append(p)
    unique = []
    for p in ordered:
        if p not in unique:
            unique.append(p)
    if len(unique) < 2:
        return None
    return _idf_segment_key(unique[0], unique[1])


def _idf_order_walls_by_connectivity(wall_uids, face_by_uid: dict[str, _IDFUnifiedFace]) -> list[str]:
    wall_uids = list(wall_uids)
    if len(wall_uids) <= 2:
        return wall_uids

    segments: dict[str, tuple[tuple[float, float], tuple[float, float]]] = {}
    for uid in wall_uids:
        face = face_by_uid.get(uid)
        if face is None:
            continue
        seg = _idf_face_bottom_segment(face)
        if seg is not None:
            segments[uid] = seg

    if len(segments) < 2:
        return wall_uids

    unvisited = [uid for uid in wall_uids if uid in segments]
    ordered = []
    first = unvisited.pop(0)
    ordered.append(first)
    a, b = segments[first]
    prev_point, current_point = a, b

    while unvisited:
        idx = None
        chosen_uid = None
        next_point = None

        for i, uid in enumerate(unvisited):
            s1, s2 = segments[uid]
            if s1 == current_point or s2 == current_point:
                candidate_next = s2 if s1 == current_point else s1
                if candidate_next != prev_point:
                    idx = i
                    chosen_uid = uid
                    next_point = candidate_next
                    break

        if chosen_uid is None:
            for i, uid in enumerate(unvisited):
                s1, s2 = segments[uid]
                if s1 == current_point or s2 == current_point:
                    idx = i
                    chosen_uid = uid
                    next_point = s2 if s1 == current_point else s1
                    break

        if chosen_uid is None:
            break

        ordered.append(chosen_uid)
        unvisited.pop(idx)
        prev_point, current_point = current_point, next_point

    tail = [uid for uid in wall_uids if uid not in ordered]
    return ordered + tail


def _idf_floor_boundary_segments(face: _IDFUnifiedFace, precision: int = 3) -> set[tuple[tuple[float, float], tuple[float, float]]]:
    pts = [_idf_point2d(v, precision) for v in face.vertices]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    out: set[tuple[tuple[float, float], tuple[float, float]]] = set()
    if len(pts) < 3:
        return out
    for i in range(len(pts)):
        a = pts[i]
        b = pts[(i + 1) % len(pts)]
        if a != b:
            out.add(_idf_segment_key(a, b))
    return out


def _idf_write_geo(outputPath: str, faces: list[_IDFUnifiedFace]) -> None:
    path.checkBuildDir(outputPath)
    lines = []
    for face in faces:
        lines.append(f"f,{face.cat},{face.geo_id}")
        nx, ny, nz = face.normal
        lines.append(f"fn,{nx},{ny},{nz}")
        for x, y, z in face.vertices:
            lines.append(f"fv,{x},{y},{z}")
        lines.append(";")
    with open(outputPath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _idf_write_xml(outputPath: str, faces: list[_IDFUnifiedFace], rec_to_face: dict[str, _IDFUnifiedFace], records: list[_IDFSurfaceRecord]) -> None:
    from ....utils import ET

    path.checkBuildDir(outputPath)
    root = ET.Element("model")
    face_by_uid = {f.uid: f for f in faces}
    zone_topology = defaultdict(lambda: {"floor": set(), "ceiling": set(), "wall": set(), "setting": {}})

    def _resolve_face_level(face: _IDFUnifiedFace) -> float:
        # Keep glazing/skylight level aligned with parent surface level so the
        # downstream glazing-to-wall matching by level can find candidates.
        if face.cat in {1, 5, 6}:
            for rec in face.records:
                parent_name = str(rec.parent_surface).strip()
                if parent_name and parent_name in rec_to_face:
                    return rec_to_face[parent_name].level
        return face.level

    for face in faces:
        level = str(_resolve_face_level(face))
        offset = str(face.offset)
        if face.cat == -1:
            node = ET.SubElement(root, "shading")
            ET.SubElement(node, "Uid").text = face.uid
            ET.SubElement(node, "faceId").text = face.geo_id
            ET.SubElement(node, "normal").text = " ".join(str(value) for value in face.normal)
            continue
        if face.cat in {4}:
            node = ET.SubElement(root, "face")
        elif face.cat in {2, 3, 0}:
            node = ET.SubElement(root, "wall")
        elif face.cat in {5, 1}:
            node = ET.SubElement(root, "glazing")
        elif face.cat == 6:
            node = ET.SubElement(root, "skylight")
        else:
            node = ET.SubElement(root, "wall")

        ET.SubElement(node, "Uid").text = face.uid
        ET.SubElement(node, "faceId").text = face.geo_id
        ET.SubElement(node, "level").text = level
        ET.SubElement(node, "offset").text = offset

    for rec in records:
        if rec.family != "building":
            continue
        if rec.zone_name == "" or rec.name not in rec_to_face:
            continue
        uf = rec_to_face[rec.name]
        ztp = zone_topology[rec.zone_name]
        st = rec.surface_type.upper()
        bc = rec.boundary_condition.upper()

        if st == "FLOOR":
            ztp["floor"].add(uf.uid)
        elif st in {"CEILING", "ROOF"}:
            ztp["ceiling"].add(uf.uid)
        elif uf.cat in {2, 3, 0}:
            if bc != "SURFACE":
                ztp["wall"].add(uf.uid)
        else:
            if (not uf.is_horizontal) and (bc != "SURFACE"):
                ztp["wall"].add(uf.uid)

        ztp["setting"]["zone_name"] = rec.zone_name

    for zone_name, topology in zone_topology.items():
        floor_boundary = set()
        for f_uid in topology["floor"]:
            floor_face = face_by_uid.get(f_uid)
            if floor_face is None:
                continue
            floor_boundary |= _idf_floor_boundary_segments(floor_face)

        if floor_boundary:
            filtered_walls = set()
            for w_uid in topology["wall"]:
                w_face = face_by_uid.get(w_uid)
                if w_face is None:
                    continue
                seg = _idf_face_bottom_segment(w_face)
                if seg and seg in floor_boundary:
                    filtered_walls.add(w_uid)
            if filtered_walls:
                topology["wall"] = filtered_walls

        topology["wall"] = _idf_order_walls_by_connectivity(topology["wall"], face_by_uid)

    for zone_name, topology in zone_topology.items():
        sp = ET.SubElement(root, "space")
        ET.SubElement(sp, "id").text = zone_name
        ET.SubElement(sp, "area").text = "0"
        ET.SubElement(sp, "height").text = "0"
        ET.SubElement(sp, "is_void").text = "False"
        ET.SubElement(sp, "void").text = ""

        setting = ET.SubElement(sp, "setting")
        if topology["setting"]:
            for k, v in sorted(topology["setting"].items()):
                ET.SubElement(setting, k).text = str(v)
        else:
            ET.SubElement(setting, "zone_name").text = zone_name

        topo = ET.SubElement(sp, "topology")
        if topology["floor"]:
            floor = ET.SubElement(topo, "floor")
            ET.SubElement(floor, "face").text = " ".join(sorted(topology["floor"]))
        if topology["ceiling"]:
            ceiling = ET.SubElement(topo, "ceiling")
            ET.SubElement(ceiling, "face").text = " ".join(sorted(topology["ceiling"]))
        edge = ET.SubElement(topo, "edge")
        for uid in topology["wall"]:
            w = ET.SubElement(edge, "wall")
            ET.SubElement(w, "Uid").text = uid

    levels = sorted({f.level for f in faces if f.cat == 4})
    ET.SubElement(root, "level").text = " ".join(str(v) for v in levels)
    tree = ET.ElementTree(root)
    tree.write(outputPath)


def _idf_build_artifacts(idfPath: str) -> tuple[list[_IDFSurfaceRecord], list[_IDFUnifiedFace], dict[str, _IDFUnifiedFace], dict[str, int]]:
    records = _idf_read_surfaces(idfPath)
    faces, rec_to_face, stats = _idf_unify_faces(records)
    return records, faces, rec_to_face, stats


def IDFtoGeo(idfPath: str, outputPath: str = None) -> None:
    """Export Moosas GEO from an IDF file."""
    if outputPath is None:
        outputPath = _idf_default_output_path(idfPath, ".geo")
    _, faces, _, _ = _idf_build_artifacts(idfPath)
    _idf_write_geo(outputPath, faces)


def IDFtoXml(idfPath: str, outputPath: str = None) -> None:
    """Export Moosas XML topology from an IDF file."""
    if outputPath is None:
        outputPath = _idf_default_output_path(idfPath, ".xml")
    records, faces, rec_to_face, _ = _idf_build_artifacts(idfPath)
    _idf_write_xml(outputPath, faces, rec_to_face, records)


def _idf_collect_zone_to_space_mapping(model: MoosasModel) -> dict[str, list[str]]:
    """Build zoneName->spaceIds mapping from loaded spaces.

    Priority:
    1) space.settings['zone_name'] generated during IDF->XML conversion
    2) fallback to current space.id when zone_name is unavailable
    """
    zoneMap = defaultdict(list)
    for space in model.spaceList:
        zoneName = ""
        if hasattr(space, "settings") and isinstance(space.settings, dict):
            zoneName = str(space.settings.get("zone_name", "")).strip()
        if zoneName == "":
            zoneName = str(space.id)
        zoneMap[zoneName].append(str(space.id))
    return _normalize_zone_name_to_space_dict(dict(zoneMap))


def _idf_apply_zone_templates(model: MoosasModel, idfPath: str) -> tuple[dict[str, list[str]], dict[str, parser.ZoneTemplate]]:
    """Build IDF zone mappings and templates without modifying the model."""
    zoneMap = _idf_collect_zone_to_space_mapping(model)
    templatesBySpaceId = {}

    for zoneName, spaceIds in zoneMap.items():
        if len(spaceIds) == 0:
            continue
        templates = loadIDFTemplate(
            model,
            idfTemplatePath=idfPath,
            spaceIds=spaceIds,
            zoneName=zoneName,
        )
        if not templates:
            raise ValueError(f"No IDF template was loaded for zone '{zoneName}'")
        templatesBySpaceId.update(templates)
    return dict(zoneMap), templatesBySpaceId


def readIDF(idfPath: str, geoPath: str = None, xmlPath: str = None) -> IDFConversionResult:
    """Convert IDF to a model plus independent IDF-specific state."""
    from ....transform.alignment import IDFtoOWL, link_idf_graph_to_moosas

    def build_result(model: MoosasModel) -> IDFConversionResult:
        zone_map, templates = _idf_apply_zone_templates(model, idfPath)
        linked_graph, uri_map = link_idf_graph_to_moosas(IDFtoOWL(idfPath), model)
        return IDFConversionResult(
            model=model,
            zone_to_space_ids=zone_map,
            templates_by_space_id=templates,
            graph=linked_graph,
            graph_source=os.path.abspath(idfPath),
            uri_map=uri_map,
        )

    if geoPath is not None and xmlPath is not None:
        IDFtoGeo(idfPath, geoPath)
        IDFtoXml(idfPath, xmlPath)
        model = loadXml(xmlPath, geoPath)
        return build_result(model)

    with tempfile.TemporaryDirectory(prefix="moosas_idf_") as tmpdir:
        temp_geo = geoPath or os.path.join(tmpdir, "from_idf.geo")
        temp_xml = xmlPath or os.path.join(tmpdir, "from_idf.xml")
        IDFtoGeo(idfPath, temp_geo)
        IDFtoXml(idfPath, temp_xml)
        model = loadXml(temp_xml, temp_geo)
        return build_result(model)
