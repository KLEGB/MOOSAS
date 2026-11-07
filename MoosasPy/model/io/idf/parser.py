import re
import os
import hashlib

from eppy.modeleditor import IDF

from .version import bundled_template_idf_path, configure_idd, require_idf_version

from .construction import Construction
from .model import *
from ...space import MoosasSpace
from ....utils import path


class ZoneTemplate():
    __slots__ = ("idf", "zoneObject", "objectList", "constructionList", "scheduleList", "hvac_mode")
    FALLBACK_ZONE_HEIGHT = 3.5

    def __init__(self, idf, zoneObject, objectList, constructionList, scheduleList, hvac_mode="ideal_loads"):
        self.idf = idf
        self.zoneObject = copy.deepcopy(zoneObject)
        self.objectList = copy.deepcopy(objectList)
        self.constructionList = copy.deepcopy(constructionList)
        self.scheduleList = copy.deepcopy(scheduleList)
        self.hvac_mode = hvac_mode

    @staticmethod
    def _match_zone_related_object(idfObject, zoneName: str):
        """
        Check whether an IDF object is related to a target zone by common zone-name fields.
        """
        zoneFields = [
            'Zone_or_ZoneList_Name',
            'Zone_or_ZoneList_or_Space_or_SpaceList_Name',
            'Zone_or_Space_Name',
            'Zone_Name',
            'Name',
        ]
        for field in zoneFields:
            if field in idfObject.objls:
                fieldValue = str(idfObject[field]).strip()
                if fieldValue == zoneName:
                    return True
        return False

    def isEmpty(self):
        """
        Return whether this template carries no valid zone geometry settings.
        """
        if not isinstance(self.zoneObject, MoosasSettings):
            return True
        if ('Floor_Area' in self.zoneObject.params) or ('Volume' in self.zoneObject.params):
            return False
        # Many EnergyPlus files leave Zone floor area and volume blank.  The
        # writer can derive them from BuildingSurface:Detailed geometry.
        area, volume, height = self._infer_zone_geometry()
        return not any((area, volume, height))

    @staticmethod
    def _as_positive_float(value):
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        return numeric if numeric > 0 else None

    def _infer_zone_geometry(self):
        zone_name = str(self.zoneObject.params.get("Name", "")).strip()
        if zone_name == "":
            return None, None, None

        area = None
        volume = self._as_positive_float(self.zoneObject.params.get("Volume"))
        height = self._as_positive_float(self.zoneObject.params.get("Ceiling_Height"))

        z_values = []
        floor_areas = []
        for surface in self.idf.idfobjects['BuildingSurface:Detailed']:
            try:
                if str(surface['Zone_Name']).strip() != zone_name:
                    continue
            except Exception:
                continue

            vertices = []
            for idx in range(1, 256):
                x_field = f'Vertex_{idx}_Xcoordinate'
                y_field = f'Vertex_{idx}_Ycoordinate'
                z_field = f'Vertex_{idx}_Zcoordinate'
                if x_field not in surface.objls or y_field not in surface.objls or z_field not in surface.objls:
                    break
                x_val, y_val, z_val = surface[x_field], surface[y_field], surface[z_field]
                if x_val == '' or y_val == '' or z_val == '':
                    break
                try:
                    vertices.append((float(x_val), float(y_val)))
                    z_values.append(float(z_val))
                except (TypeError, ValueError):
                    continue

            if str(surface['Surface_Type']).strip().upper() == 'FLOOR' and len(vertices) >= 3:
                floor_areas.append(abs(sum(
                    vertices[index][0] * vertices[(index + 1) % len(vertices)][1]
                    - vertices[(index + 1) % len(vertices)][0] * vertices[index][1]
                    for index in range(len(vertices))
                )) / 2)

        if area is None and floor_areas:
            area = sum(floor_areas)

        if height is None and len(z_values) >= 2:
            inferred_height = max(z_values) - min(z_values)
            if inferred_height > 0:
                height = inferred_height

        if area is not None and volume is not None and height is None:
            height = volume / area
        elif area is not None and height is not None and volume is None:
            volume = area * height
        elif volume is not None and height is not None and area is None:
            area = volume / height

        return area, volume, height

    @classmethod
    def fromIDF(cls, idf: IDF, zoneName: str = "", hvac_mode: str = "ideal_loads"):
        """
        Initialize the object by extracting and processing construction and zone-related data from an IDF file.
        
        Parameters
        ----------
        idf : IDF
            The IDF object containing the building energy model data, used to extract constructions, zones, 
            and related objects for further processing.
        
        Returns
        -------
        ZoneTemplate

        """
        constructionList: list[Construction] = []
        for obj in idf.idfobjects['Construction']:
            con = Construction.fromIDFConstructionList(idf, obj)
            if con:
                constructionList.append(con)
        templateZone = None
        for zoneObj in idf.idfobjects['Zone']:
            if str(zoneObj['Name']).strip() == zoneName:
                templateZone = zoneObj
                break

        if templateZone is None:
            print(f"******Warning: no Zone found with Name='{zoneName}' in template IDF")
            return cls(idf, MoosasSettings({}), {}, constructionList, {}, hvac_mode)

        zoneObject = MoosasSettings.fromIdfObject(templateZone)

        # Extract zone objects
        objectHint = ['ZoneInfiltration:DesignFlowRate',
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
                      'ThermostatSetpoint:DualSetpoint']
        objectList = {}
        sourceObjectList = {}
        found, unfound = [], []
        for objHint in objectHint:
            selectedObj = None
            for refObj in idf.idfobjects[objHint]:
                if cls._match_zone_related_object(refObj, zoneName):
                    selectedObj = refObj
                    break

            if selectedObj is None and len(idf.idfobjects[objHint]) > 0:
                selectedObj = idf.idfobjects[objHint][0]

            if selectedObj is None:
                unfound.append(objHint)
                continue

            template = MoosasSettings.fromIdfObject(selectedObj)
            objectList[objHint] = template
            sourceObjectList[objHint] = selectedObj
            found.append(objHint)

        equipment_connections = next((
            obj for obj in idf.idfobjects['ZoneHVAC:EquipmentConnections']
            if str(obj['Zone_Name']).strip() == zoneName
        ), None) if hvac_mode == "simple" else None
        if equipment_connections is not None:
            equipment_list_name = str(
                equipment_connections['Zone_Conditioning_Equipment_List_Name']
            ).strip()
            equipment_list = next((
                obj for obj in idf.idfobjects['ZoneHVAC:EquipmentList']
                if str(obj['Name']).strip() == equipment_list_name
            ), None)
            if equipment_list is None:
                raise ValueError(
                    f"Zone '{zoneName}' references missing ZoneHVAC:EquipmentList "
                    f"'{equipment_list_name}'"
                )

            equipment_refs = []
            equipment_index = 1
            while f'Zone_Equipment_{equipment_index}_Object_Type' in equipment_list.objls:
                equipment_type = str(
                    equipment_list[f'Zone_Equipment_{equipment_index}_Object_Type']
                ).strip()
                equipment_name = str(
                    equipment_list[f'Zone_Equipment_{equipment_index}_Name']
                ).strip()
                if equipment_type or equipment_name:
                    equipment_refs.append((equipment_type, equipment_name))
                equipment_index += 1

            if len(equipment_refs) != 1:
                raise ValueError(
                    f"Simple HVAC migration requires exactly one zone equipment object for "
                    f"Zone '{zoneName}'; found {len(equipment_refs)}"
                )

            supported_equipment = {
                'ZONEHVAC:BASEBOARD:CONVECTIVE:ELECTRIC': 'ZoneHVAC:Baseboard:Convective:Electric',
                'ZONEHVAC:IDEALLOADSAIRSYSTEM': 'ZoneHVAC:IdealLoadsAirSystem',
            }
            source_type, source_name = equipment_refs[0]
            equipment_type = supported_equipment.get(source_type.upper())
            if equipment_type is None:
                raise ValueError(
                    f"Simple HVAC migration does not support '{source_type}' in Zone '{zoneName}'"
                )
            equipment = next((
                obj for obj in idf.idfobjects[equipment_type]
                if str(obj['Name']).strip() == source_name
            ), None)
            if equipment is None:
                raise ValueError(
                    f"Zone '{zoneName}' references missing {equipment_type} '{source_name}'"
                )

            for object_type, source_object in (
                ('ZoneHVAC:EquipmentConnections', equipment_connections),
                ('ZoneHVAC:EquipmentList', equipment_list),
                (equipment_type, equipment),
            ):
                objectList[object_type] = MoosasSettings.fromIdfObject(source_object)
                sourceObjectList[object_type] = source_object
                found.append(object_type)

            if equipment_type == 'ZoneHVAC:IdealLoadsAirSystem':
                node_list_name = str(
                    equipment_connections['Zone_Air_Inlet_Node_or_NodeList_Name']
                ).strip()
                node_list = next((
                    obj for obj in idf.idfobjects['NodeList']
                    if str(obj['Name']).strip() == node_list_name
                ), None)
                if node_list is not None:
                    objectList['NodeList'] = MoosasSettings.fromIdfObject(node_list)
                    sourceObjectList['NodeList'] = node_list
                    found.append('NodeList')
        print('foundObj:', found)
        print('unfoundObj:', unfound)

        # get schedules
        scheduleList = {}
        # locate schedule
        for objHint in objectList:
            scheduleList[objHint] = {}
            for field in objectList[objHint].params.keys():
                if re.search("Schedule_Name", field, re.IGNORECASE):
                    try:
                        ref_obj = sourceObjectList[objHint].get_referenced_object(field)
                    except Exception:
                        ref_obj = None
                    if ref_obj is None:
                        continue
                    scheduleList[objHint][field] = MoosasSettings.fromIdfObject(ref_obj)
                    scheduleList[objHint][field].updateParams(**{"Name": ""})

        return cls(idf, zoneObject, objectList, constructionList, scheduleList, hvac_mode)

    @classmethod
    def createFromZone(cls, zone: MoosasSpace, idfTemplatePath=None, baseTemplate=None, zoneName: str = ""):
        """
        Build a ZoneTemplate by reversing zone settings back to IDF template objects.
        """
        if baseTemplate is None:
            configure_idd()
            idfTemplatePath = require_idf_version(idfTemplatePath or bundled_template_idf_path())
            baseIDF = IDF(idfTemplatePath)
            baseTemplate = cls.fromIDF(baseIDF, zoneName=zoneName)

        template = cls(baseTemplate.idf,
                       baseTemplate.zoneObject,
                       baseTemplate.objectList,
                       baseTemplate.constructionList,
                       baseTemplate.scheduleList,
                       baseTemplate.hvac_mode)

        if template.isEmpty():
            return template

        zoneArea = max(float(zone.area), 1e-6)
        zoneHeight = max(float(zone.height), 1e-6)
        zoneVolume = zoneArea * zoneHeight

        def _setting(key, fallback):
            if key in zone.settings:
                try:
                    return float(zone.settings[key])
                except (TypeError, ValueError):
                    return fallback
            return fallback

        zoneInfiltration = _setting('zone_infiltration', SpaceDefault['zone_infiltration'])
        zonePPSM = _setting('zone_ppsm', SpaceDefault['zone_ppsm'])
        zoneEquipment = _setting('zone_equipment', SpaceDefault['zone_equipment'])
        zoneLighting = _setting('zone_lighting', SpaceDefault['zone_lighting'])
        zonePFAV = _setting('zone_pfav', SpaceDefault['zone_pfav'])

        template.zoneObject.updateParams(**{
            'Name': '',
            'Floor_Area': zoneArea,
            'Volume': zoneVolume,
            'Ceiling_Height': zoneHeight,
        })

        if 'ZoneInfiltration:DesignFlowRate' in template.objectList:
            template.objectList['ZoneInfiltration:DesignFlowRate'].updateParams(**{
                'Name': '',
                'Zone_or_ZoneList_Name': '',
                'Zone_or_ZoneList_or_Space_or_SpaceList_Name': '',
                'Design_Flow_Rate_Calculation_Method': 'Flow/Zone',
                'Design_Flow_Rate': zoneInfiltration / 3600.0 * zoneVolume,
            })

        if 'ZoneVentilation:DesignFlowRate' in template.objectList:
            template.objectList['ZoneVentilation:DesignFlowRate'].updateParams(**{
                'Name': '',
                'Zone_or_ZoneList_Name': '',
                'Zone_or_ZoneList_or_Space_or_SpaceList_Name': '',
                'Flow_Rate_per_Person': zonePFAV / 3600.0,
            })

        if 'ZoneVentilation:WindandStackOpenArea' in template.objectList:
            template.objectList['ZoneVentilation:WindandStackOpenArea'].updateParams(**{
                'Name': '',
                'Zone_or_Space_Name': '',
                'Zone_or_ZoneList_or_Space_or_SpaceList_Name': '',
            })

        if 'OtherEquipment' in template.objectList:
            template.objectList['OtherEquipment'].updateParams(**{
                'Name': '',
                'Zone_or_ZoneList_Name': '',
                'Zone_or_ZoneList_or_Space_or_SpaceList_Name': '',
                'Power_per_Zone_Floor_Area': zoneEquipment,
                'Watts_per_Floor_Area': zoneEquipment,
            })

        if 'ElectricEquipment' in template.objectList:
            template.objectList['ElectricEquipment'].updateParams(**{
                'Name': '',
                'Zone_or_ZoneList_Name': '',
                'Zone_or_ZoneList_or_Space_or_SpaceList_Name': '',
                'Watts_per_Zone_Floor_Area': zoneEquipment,
                'Watts_per_Floor_Area': zoneEquipment,
            })

        if 'People' in template.objectList:
            template.objectList['People'].updateParams(**{
                'Name': '',
                'Zone_or_ZoneList_Name': '',
                'Zone_or_ZoneList_or_Space_or_SpaceList_Name': '',
                'People_per_Zone_Floor_Area': zonePPSM,
                'People_per_Floor_Area': zonePPSM,
            })

        if 'Lights' in template.objectList:
            template.objectList['Lights'].updateParams(**{
                'Name': '',
                'Zone_or_ZoneList_Name': '',
                'Zone_or_ZoneList_or_Space_or_SpaceList_Name': '',
                'Watts_per_Zone_Floor_Area': zoneLighting,
                'Watts_per_Floor_Area': zoneLighting,
            })

        if 'Sizing:Zone' in template.objectList:
            template.objectList['Sizing:Zone'].updateParams(**{
                'Zone_or_ZoneList_Name': '',
                'Zone_or_ZoneList_or_Space_or_SpaceList_Name': '',
                'Design_Specification_Outdoor_Air_Object_Name': '',
                'Design_Specification_Zone_Air_Distribution_Object_Name': '',
            })

        if 'DesignSpecification:OutdoorAir' in template.objectList:
            template.objectList['DesignSpecification:OutdoorAir'].updateParams(**{
                'Name': '',
                'Outdoor_Air_Flow_per_Person': zonePFAV / 3600.0,
            })

        if 'DesignSpecification:ZoneAirDistribution' in template.objectList:
            template.objectList['DesignSpecification:ZoneAirDistribution'].updateParams(**{'Name': ''})

        if 'ZoneControl:Thermostat' in template.objectList:
            template.objectList['ZoneControl:Thermostat'].updateParams(**{
                'Name': '',
                'Zone_or_ZoneList_Name': '',
                'Zone_or_ZoneList_or_Space_or_SpaceList_Name': '',
                'Control_1_Name': '',
            })

        if 'ThermostatSetpoint:DualSetpoint' in template.objectList:
            template.objectList['ThermostatSetpoint:DualSetpoint'].updateParams(**{'Name': ''})

        if 'ZoneHVAC:EquipmentConnections' in template.objectList:
            template.objectList['ZoneHVAC:EquipmentConnections'].updateParams(**{
                'Zone_Name': '',
                'Zone_Conditioning_Equipment_List_Name': '',
                'Zone_Air_Inlet_Node_or_NodeList_Name': '',
                'Zone_Air_Node_Name': '',
                'Zone_Return_Air_Node_or_NodeList_Name': '',
                'Zone_Air_Exhaust_Node_or_NodeList_Name': '',
            })

        if 'ZoneHVAC:EquipmentList' in template.objectList:
            template.objectList['ZoneHVAC:EquipmentList'].updateParams(**{
                'Name': '',
                'Zone_Equipment_1_Name': '',
            })

        if 'ZoneHVAC:IdealLoadsAirSystem' in template.objectList:
            template.objectList['ZoneHVAC:IdealLoadsAirSystem'].updateParams(**{
                'Name': '',
                'Zone_Supply_Air_Node_Name': '',
                'Zone_Exhaust_Air_Node_Name': '',
                'Design_Specification_Outdoor_Air_Object_Name': '',
            })

        if 'NodeList' in template.objectList:
            template.objectList['NodeList'].updateParams(**{
                'Name': '',
                'Node_1_Name': '',
            })

        for objHint in template.scheduleList:
            for field in template.scheduleList[objHint]:
                template.scheduleList[objHint][field].updateParams(**{'Name': ''})

        return template

    def getConstruction(self, _type, UFactor, SHGC=None):
        """
        Find or create a construction by type and U-factor.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the construction list and IDF.
        _type : str
            The type of construction to find or create.
        UFactor : float or str
            The U-factor value for the construction; will be converted to float.
        SHGC : float, optional
            The Solar Heat Gain Coefficient (SHGC) for the new construction. Default is None.
        
        Returns
        -------
        Construction
            The existing construction with closest U-factor match or a newly created 
            and added Construction object.
        """
        UFactor = float(UFactor)
        constr = [construction for construction in self.constructionList if construction.type == _type]
        if len(constr) > 0:
            # Reuse only an equivalent construction. Selecting the nearest one
            # silently discarded per-wall/window edits.
            for construction in constr:
                if abs(float(construction.UFactor) - UFactor) < 1e-8:
                    if _type != 'window' or SHGC is None or abs(float(construction.layers[0].params.get('Solar_Heat_Gain_Coefficient', SHGC)) - float(SHGC)) < 1e-8:
                        return construction

        # Template constructions are reused above whenever their thermal
        # properties match. New objects need deterministic names because
        # short random names collide in large exports.
        signature = "|".join([
            _type,
            f"{UFactor:.8f}",
            "" if SHGC is None else f"{float(SHGC):.8f}",
        ])
        digest = hashlib.sha1(signature.encode("ascii")).hexdigest()[:12]
        prefix = "MoosasWindow" if _type == "window" else "MoosasOpaque"
        construction_name = self._unique_idf_name(
            "Construction",
            f"{prefix}_{digest}",
        )
        layer_type = "WindowMaterial:SimpleGlazingSystem" if _type == "window" else "Material"
        layer_prefix = "MoosasGlazingMaterial" if _type == "window" else "MoosasOpaqueMaterial"
        layer_name = self._unique_idf_name(
            layer_type,
            f"{layer_prefix}_{digest}",
        )
        construction = Construction.create(
            _type=_type,
            UFactor=UFactor,
            SHGC=SHGC,
            construction_name=construction_name,
            layer_name=layer_name,
        )
        construction.applyToIDF(self.idf)
        self.constructionList.append(construction)
        return construction

    def _unique_idf_name(self, object_type, base_name):
        """Return a stable name that is unused in the current IDF object type."""
        existing = {
            str(obj["Name"]).strip()
            for obj in self.idf.idfobjects[object_type]
            if str(obj["Name"]).strip()
        }
        candidate = base_name
        suffix = 2
        while candidate in existing:
            candidate = f"{base_name}_{suffix}"
            suffix += 1
        return candidate

    def appliedToZone(self, zone: MoosasSpace):
        """
        Apply zone-specific settings and schedules to an IDF model.
        
        Parameters
        ----------
        zone : MoosasSpace
            A zone object containing settings such as work hours, temperature setpoints,
            occupancy, equipment, lighting, infiltration, ventilation, and other zone-level
            parameters. The settings are used to construct schedules and apply HVAC and load
            specifications.
        
        Returns
        -------
        ZoneTemplate
            This function return a new Zone object containing settings to the zone.
        """

        if not zone.conditioned:
            self.zoneObject.updateParams(
                **{'Name': zone.id, 'Floor_Area': zone.area, 'Volume': zone.area * zone.height}
            )
            return ZoneTemplate(self.idf, self.zoneObject, {}, self.constructionList, {}, self.hvac_mode)

        # rename and update Schedule
        for objHint in self.scheduleList:
            for field in self.scheduleList[objHint]:
                schName = zone.id + objHint + field
                self.scheduleList[objHint][field].updateParams(**{"Name": schName})
                self.objectList[objHint].updateParams(**{field: schName})

        # get zone property
        zoneTemplateArea = self._as_positive_float(self.zoneObject.params.get("Floor_Area"))
        zoneTemplateVolume = self._as_positive_float(self.zoneObject.params.get("Volume"))
        zoneTemplateHeight = self._as_positive_float(self.zoneObject.params.get("Ceiling_Height"))
        if zoneTemplateArea and zoneTemplateVolume:
            zoneTemplateHeight = zoneTemplateVolume / zoneTemplateArea
        elif zoneTemplateArea and zoneTemplateHeight:
            zoneTemplateVolume = zoneTemplateArea * zoneTemplateHeight
        elif zoneTemplateVolume and zoneTemplateHeight:
            zoneTemplateArea = zoneTemplateVolume / zoneTemplateHeight
        else:
            zoneTemplateArea, zoneTemplateVolume, zoneTemplateHeight = self._infer_zone_geometry()
            if zoneTemplateVolume and not zoneTemplateArea:
                zoneTemplateHeight = self.FALLBACK_ZONE_HEIGHT
                zoneTemplateArea = zoneTemplateVolume / zoneTemplateHeight
                print(
                    f"******Warning: fallback zone geometry activated for '{zone.id}'; "
                    f"using height={zoneTemplateHeight}m to infer floor area from template volume"
                )
            if not (zoneTemplateArea and zoneTemplateVolume and zoneTemplateHeight):
                raise ValueError("idf does not contain valid zone-specific settings: height/area/volume")

        zoneOutGlazingArea = (sum([gls.area for wall in zone.edge.wall if wall.isOuter for gls in
                                   wall.glazingElement]) + sum(
            [gls.area for face in zone.ceiling.face if face.isOuter for gls in face.glazingElement]))
        zoneOutWallArea = (sum([wall.area for wall in zone.edge.wall if wall.isOuter]) + sum(
            [face.area for face in zone.ceiling.face if face.isOuter]))

        # zone_infiltration
        if "ZoneInfiltration:DesignFlowRate" in self.objectList:
            if 'Design_Flow_Rate' in self.objectList["ZoneInfiltration:DesignFlowRate"].params:
                inftM3s = self.objectList["ZoneInfiltration:DesignFlowRate"]['Design_Flow_Rate']  # {m3/s}
                zone.settings['zone_infiltration'] = inftM3s / zoneTemplateVolume * 3600  # ac/h
            if 'Flow_Rate_per_Floor_Area' in self.objectList["ZoneInfiltration:DesignFlowRate"].params:
                inftM3sM2 = self.objectList["ZoneInfiltration:DesignFlowRate"]['Flow_Rate_per_Floor_Area']  # {m3/s-m2}
                zone.settings['zone_infiltration'] = inftM3sM2 / zoneTemplateHeight * 3600  # ac/h
            exterior_surface_field = next((field for field in (
                'Flow_Rate_per_Exterior_Surface_Area',
                'Flow_per_Exterior_Surface_Area',
                'Flow_Rate_per_Exterior_Wall_Area',
                'Flow_per_Exterior_Wall_Area',
            ) if field in self.objectList["ZoneInfiltration:DesignFlowRate"].params), None)
            if exterior_surface_field:
                inftM3sM2 = self.objectList["ZoneInfiltration:DesignFlowRate"][exterior_surface_field]  # {m3/s-m2}
                zone.settings[
                    'zone_infiltration'] = inftM3sM2 * zoneOutWallArea / zone.height / zone.area * 3600  # ac/h
        zone.settings['zone_infiltration'] = float(zone.settings['zone_infiltration'])

        # population
        if 'People' in self.objectList:
            if 'Number_of_People' in self.objectList["People"].params:
                zone.settings['zone_ppsm'] = self.objectList["People"]["Number_of_People"] / zoneTemplateArea
            if 'People_per_Floor_Area' in self.objectList["People"].params:
                zone.settings['zone_ppsm'] = self.objectList["People"]["People_per_Floor_Area"]
            if 'People_per_Zone_Floor_Area' in self.objectList["People"].params:
                zone.settings['zone_ppsm'] = self.objectList["People"]["People_per_Zone_Floor_Area"]
            if 'Floor_Area_per_Person' in self.objectList["People"].params:
                zone.settings['zone_ppsm'] = 1 / self.objectList["People"]["Floor_Area_per_Person"]
            if 'Zone_Floor_Area_per_Person' in self.objectList["People"].params:
                zone.settings['zone_ppsm'] = 1 / self.objectList["People"]["Zone_Floor_Area_per_Person"]
        zone.settings['zone_ppsm'] = float(zone.settings['zone_ppsm'])

        # equipment (equip elec)
        if 'ElectricEquipment' in self.objectList:
            if 'Design_Level' in self.objectList["ElectricEquipment"].params:
                zone.settings['zone_equipment'] = self.objectList["ElectricEquipment"][
                                                      'Design_Level'] / zoneTemplateArea
            if 'Watts_per_Floor_Area' in self.objectList["ElectricEquipment"].params:
                zone.settings['zone_equipment'] = self.objectList["ElectricEquipment"]["Watts_per_Floor_Area"]
            if 'Watts_per_Zone_Floor_Area' in self.objectList["ElectricEquipment"].params:
                zone.settings['zone_equipment'] = self.objectList["ElectricEquipment"]["Watts_per_Zone_Floor_Area"]
            if 'Watts_per_Person' in self.objectList["ElectricEquipment"].params:
                zone.settings['zone_equipment'] = self.objectList["ElectricEquipment"]["Watts_per_Person"] * \
                                                  zone.settings['zone_ppsm']

        # equipment (equip heat)
        if 'OtherEquipment' in self.objectList:
            if 'Design_Level' in self.objectList["OtherEquipment"].params:
                zone.settings['zone_equipment'] = self.objectList["OtherEquipment"]['Design_Level'] / zoneTemplateArea
            if 'Watts_per_Floor_Area' in self.objectList["OtherEquipment"].params:
                zone.settings['zone_equipment'] = self.objectList["OtherEquipment"]["Watts_per_Floor_Area"]
            if 'Watts_per_Person' in self.objectList["OtherEquipment"].params:
                zone.settings['zone_equipment'] = self.objectList["OtherEquipment"]["Watts_per_Person"] * zone.settings[
                    'zone_ppsm']
        zone.settings['zone_equipment'] = float(zone.settings['zone_equipment'])

        # light

        if 'Lights' in self.objectList:
            if 'Lighting_Level' in self.objectList["Lights"].params:
                zone.settings['zone_lighting'] = self.objectList["Lights"]['Lighting_Level'] / zoneTemplateArea
            if 'Watts_per_Floor_Area' in self.objectList["Lights"].params:
                zone.settings['zone_lighting'] = self.objectList["Lights"]["Watts_per_Floor_Area"]
            if 'Watts_per_Zone_Floor_Area' in self.objectList["Lights"].params:
                zone.settings['zone_lighting'] = self.objectList["Lights"]["Watts_per_Zone_Floor_Area"]
            if 'Watts_per_Person' in self.objectList["Lights"].params:
                zone.settings['zone_lighting'] = self.objectList["Lights"]["Watts_per_Person"] * zone.settings[
                    'zone_ppsm']
        zone.settings['zone_lighting'] = float(zone.settings['zone_lighting'])

        # ventilation Flow_Rate_per_Person
        if 'ZoneVentilation:DesignFlowRate' in self.objectList:
            if 'Design_Flow_Rate' in self.objectList["ZoneVentilation:DesignFlowRate"].params:
                inftM3s = self.objectList["ZoneVentilation:DesignFlowRate"]['Design_Flow_Rate']  # {m3/s}
                zone.settings['zone_pfav'] = inftM3s * 3600 / zone.settings['zone_ppsm'] / zone.area  # m3/h-pp
            if 'Flow_Rate_per_Floor_Area' in self.objectList["ZoneVentilation:DesignFlowRate"].params:
                inftM3sM2 = self.objectList["ZoneVentilation:DesignFlowRate"]['Flow_Rate_per_Floor_Area']  # {m3/s-m2}
                zone.settings['zone_pfav'] = inftM3sM2 * 3600 / zone.settings['zone_ppsm']  # m3/h-pp
            if 'Flow_Rate_per_Person' in self.objectList["ZoneVentilation:DesignFlowRate"].params:
                inftM3pp = self.objectList["ZoneVentilation:DesignFlowRate"]['Flow_Rate_per_Person']  # {m3/s-pp}
                zone.settings['zone_pfav'] = inftM3pp * 3600  # m3/h-pp
            if 'Air_Changes_per_Hour' in self.objectList["ZoneVentilation:DesignFlowRate"]:
                ach = self.objectList["ZoneVentilation:DesignFlowRate"]['Air_Changes_per_Hour']  # {ac/h}
                zone.settings['zone_pfav'] = ach * zoneTemplateVolume * 3600  # m3/h-pp

        if 'DesignSpecification:OutdoorAir' in self.objectList:
            # Keep the independent flow components and method in the IDF object.
            # zone_pfav represents only its per-person component, not their total.
            outdoor_air = self.objectList['DesignSpecification:OutdoorAir'].params
            zone.settings['zone_pfav'] = float(outdoor_air.get('Outdoor_Air_Flow_per_Person', 0.00944)) * 3600
        zone.settings['zone_pfav'] = float(zone.settings['zone_pfav'])

        if self.hvac_mode == "ideal_loads":
            self.objectList.update({
                'ZoneHVAC:EquipmentConnections': MoosasSettings({'key': 'ZoneHVAC:EquipmentConnections'}),
                'ZoneHVAC:EquipmentList': MoosasSettings({
                    'key': 'ZoneHVAC:EquipmentList',
                    'Zone_Equipment_1_Object_Type': 'ZoneHVAC:IdealLoadsAirSystem',
                    'Zone_Equipment_1_Name': '',
                    'Zone_Equipment_1_Cooling_Sequence': 1,
                    'Zone_Equipment_1_Heating_or_NoLoad_Sequence': 1,
                }),
                'ZoneHVAC:IdealLoadsAirSystem': MoosasSettings({'key': 'ZoneHVAC:IdealLoadsAirSystem'}),
                'NodeList': MoosasSettings({'key': 'NodeList'}),
            })

        params = {
            'ZoneInfiltration:DesignFlowRate':
                {'Name': zone.id + '_Infiltration', 'Zone_or_ZoneList_Name': zone.id,
                 'Zone_or_ZoneList_or_Space_or_SpaceList_Name': zone.id,
                 'Design_Flow_Rate_Calculation_Method': "Flow/Zone",
                 'Design_Flow_Rate': zone.settings['zone_infiltration'] / 3600 * zone.area * zone.height},
            'ZoneVentilation:DesignFlowRate':
                {'Name': zone.id + "_Ventilation",  # Block2:Zone5 Ventilation
                 'Zone_or_ZoneList_Name': zone.id, 'Zone_or_ZoneList_or_Space_or_SpaceList_Name': zone.id,
                 "Flow_Rate_per_Person": zone.settings['zone_pfav'] / 3600},
            'ZoneVentilation:WindandStackOpenArea':
                {'Name': zone.id + '_Opening', 'Zone_or_Space_Name': zone.id,
                 'Zone_or_ZoneList_or_Space_or_SpaceList_Name': zone.id,
                 'Opening_Area': zoneOutGlazingArea * 0.6,
                 },
            'OtherEquipment':
                {'Name': zone.id + '_Equipment', 'Zone_or_ZoneList_Name': zone.id,
                 'Zone_or_ZoneList_or_Space_or_SpaceList_Name': zone.id,
                 'Power_per_Zone_Floor_Area': zone.settings['zone_equipment'],
                 },
            'ElectricEquipment':
                {'Name': zone.id + '_Equipment', 'Zone_or_ZoneList_or_Space_or_SpaceList_Name': zone.id,
                 'Watts_per_Zone_Floor_Area': zone.settings['zone_equipment'],
                 },
            'People':
                {'Name': zone.id + '_People', 'Zone_or_ZoneList_Name': zone.id,
                 'Zone_or_ZoneList_or_Space_or_SpaceList_Name': zone.id,
                 'People_per_Zone_Floor_Area': zone.settings['zone_ppsm'],
                 },
            'Lights':
                {'Name': zone.id + '_Lights', 'Zone_or_ZoneList_Name': zone.id,
                 'Zone_or_ZoneList_or_Space_or_SpaceList_Name': zone.id,
                 'Watts_per_Zone_Floor_Area': zone.settings['zone_lighting'],
                 },
            'Sizing:Zone':
                {'Zone_or_ZoneList_Name': zone.id, 'Zone_or_ZoneList_or_Space_or_SpaceList_Name': zone.id,
                 'Design_Specification_Outdoor_Air_Object_Name':
                 zone.id if 'DesignSpecification:OutdoorAir' in self.objectList else '',
                 'Design_Specification_Zone_Air_Distribution_Object_Name': ''
                 },
            'DesignSpecification:OutdoorAir':
                {'Name': zone.id},
            'DesignSpecification:ZoneAirDistribution':
                {'Name': zone.id},
            'ZoneControl:Thermostat':
                {'Name': zone.id + "_Thermostat",
                 'Zone_or_ZoneList_Name': zone.id, 'Zone_or_ZoneList_or_Space_or_SpaceList_Name': zone.id,
                 'Control_1_Name': zone.id + "_SetPoint",
                 },
            'ThermostatSetpoint:DualSetpoint':
                {'Name': zone.id + "_SetPoint",
                 },
            'ZoneHVAC:EquipmentConnections':
                {'Zone_Name': zone.id,
                 'Zone_Conditioning_Equipment_List_Name': zone.id + '_EquipmentList',
                 'Zone_Air_Node_Name': 'Node ' + zone.id + ' Zone',
                 },
            'ZoneHVAC:EquipmentList':
                {'Name': zone.id + '_EquipmentList'},
            'NodeList':
                {'Name': zone.id + " Inlets",
                 'Node_1_Name': "Node " + zone.id + " In"
                 }
        }

        equipment_list = self.objectList.get('ZoneHVAC:EquipmentList')
        if equipment_list is not None:
            equipment_type = equipment_list.params['Zone_Equipment_1_Object_Type']
            source_name = equipment_list.params['Zone_Equipment_1_Name']
            equipment_name = f'{zone.id}_{source_name}'
            params['ZoneHVAC:EquipmentList']['Zone_Equipment_1_Name'] = equipment_name
            params[equipment_type] = {'Name': equipment_name}
            if equipment_type == 'ZoneHVAC:IdealLoadsAirSystem':
                params['ZoneHVAC:EquipmentConnections'].update({
                    'Zone_Air_Inlet_Node_or_NodeList_Name': zone.id + ' Inlets',
                    'Zone_Return_Air_Node_or_NodeList_Name': 'Node ' + zone.id + ' Out',
                    'Zone_Air_Exhaust_Node_or_NodeList_Name': '',
                })
                params[equipment_type].update({
                    'Zone_Supply_Air_Node_Name': 'Node ' + zone.id + ' In',
                    'Zone_Exhaust_Air_Node_Name': '',
                    'Design_Specification_Outdoor_Air_Object_Name':
                        zone.id if 'DesignSpecification:OutdoorAir' in self.objectList else '',
                })

        for key in self.objectList:
            self.objectList[key].updateParams(**params.get(key, {}))

        # block items:
        # blockObjects = ['DesignSpecification:OutdoorAir', 'DesignSpecification:ZoneAirDistribution']
        # for item in blockObjects:
        #     if item in self.objectList:
        #         del self.objectList[item]
        #         del self.scheduleList[item]

        # create zone objects
        self.zoneObject.updateParams(
            **{'Name': zone.id, 'Floor_Area': zone.area, 'Volume': zone.area * zone.height})

        return ZoneTemplate(self.idf, self.zoneObject, self.objectList, self.constructionList, self.scheduleList, self.hvac_mode)

    def applyToIDF(self, idf=None):
        if idf == None:
            idf = self.idf
        # print(self.zoneObject)
        self.zoneObject.applyToIDF(idf)
        for objHint in self.scheduleList:
            for field in self.scheduleList[objHint]:
                self.scheduleList[objHint][field].applyToIDF(idf)
        for key in self.objectList:
            self.objectList[key].applyToIDF(idf)
