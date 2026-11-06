"""Portable gbXML thermal evidence, SI projection and strict input alignment.

Source XML and per-field XPath/unit records travel inside RDF/energy metadata.
Missing fields may use MoosasPy defaults; supplied fields cannot be replaced by
a template or runtime override without changing the external source scenario.
"""
from __future__ import annotations

from copy import deepcopy
import calendar
import hashlib
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET

from rdflib import Literal, Namespace, URIRef
from rdflib.namespace import RDF, XSD

NS = 'http://www.gbxml.org/schema'
GBE = Namespace('urn:moosas:thermal-evidence:')
DOC = URIRef('urn:moosas:external-thermal-model')
LENGTH = {'Meters': 1., 'Millimeters': .001, 'Centimeters': .01, 'Feet': .3048, 'Inches': .0254}
AREA = {'SquareMeters': 1., 'SquareFeet': .09290304}
VOLUME = {'CubicMeters': 1., 'CubicFeet': .028316846592}


def q(name):
    return f'{{{NS}}}{name}'


def units(root):
    try:
        return (LENGTH[root.get('lengthUnit', 'Meters')], AREA[root.get('areaUnit', 'SquareMeters')],
                VOLUME[root.get('volumeUnit', 'CubicMeters')])
    except KeyError as exc:
        raise ValueError(f'unsupported gbXML geometry unit: {exc}') from exc


def numeric(node, *, unit=None):
    value = float(node.text)
    if not math.isfinite(value):
        raise ValueError('nonfinite source thermal value')
    u = node.get('unit', unit or '')
    factors = {'': 1., 'WPerSquareMeterK': 1., 'WPerSquareMeter': 1., 'WPerMeterK': 1.,
               'KgPerCubicM': 1., 'JPerKgK': 1., 'SquareMeterKPerW': 1., 'Meters': 1.,
               'Millimeters': .001, 'Centimeters': .01, 'Feet': .3048, 'Inches': .0254,
               'BtuPerHourSquareFootF': 5.678263337, 'BtuPerHourFootF': 1.730734667,
               'PoundsPerCubicFt': 16.01846337, 'BtuPerPoundF': 4186.8,
               'SquareFootFPerBtu': .1761101838, 'Watt': 1., 'Watts': 1.,
               'NumberOfPeople': 1., 'PeoplePerSquareMeter': 1., 'Fraction': 1.,
               'AirChangesPerHour': 1., 'CubicMPerHour': 1., 'CubicMPerSecond': 3600.,
               'LPerSecond': 3.6, 'CubicFtPerMinute': 1.699010796,
               'WPerPerson': 1., 'JPerPersonHour': 1/3600, 'COP': 1., 'Percent': .01,
               'EER': 1/3.412141633}
    if u in {'C', 'Celsius'}:
        return value
    if u in {'F', 'Fahrenheit'}:
        return (value-32)*5/9
    if u in {'K', 'Kelvin'}:
        return value-273.15
    if u not in factors:
        raise ValueError(f'unsupported source thermal unit {u!r}')
    return value*factors[u]


def project(root, *, source_name='', source_sha256=None):
    """Resolve supported source fields; retain unsupported fields as blockers."""
    raw = ET.tostring(root, encoding='unicode')
    ids = {n.get('id'): n for n in root.iter() if n.get('id')}
    _, area_scale, volume_scale = units(root)
    surfaces, zones, schedules, records, unresolved = {}, {}, {}, [], []
    energy_defaults = {}

    def issue(owner, field, message):
        unresolved.append(dict(source_id=owner, field=field, reason=message))

    def record(owner, field, value, node, path):
        records.append(dict(source_id=owner, parameter=field, value_si=value,
                            xpath=path, original_value=node.text, original_unit=node.get('unit', ''),
                            source='gbXML'))
        return value

    def scalar(parent, tag, owner, target, key, *, unit=None, factor=1.):
        node = parent.find(q(tag))
        if node is None or not node.text:
            return
        try:
            target[key] = record(owner, key, numeric(node, unit=unit)*factor, node,
                                 f"//*[@id='{owner}']/{tag}")
        except (ValueError, TypeError) as exc:
            issue(owner, key, str(exc))

    def schedule(ident):
        if ident in schedules:
            return schedules[ident]
        node = ids.get(ident)
        if node is None:
            raise ValueError(f'missing schedule {ident}')
        def day(day_id):
            d = ids[day_id]
            values = [numeric(v, unit=d.get('unit', '')) for v in d.findall(q('ScheduleValue'))]
            if len(values) != 24:
                raise ValueError(f'{day_id}: needs 24 hourly ScheduleValue entries')
            return values
        def week(week_id):
            result = {}
            aliases = {'Weekday': range(5), 'Weekend': (5,6), 'All': range(7),
                       **{name: (i,) for i,name in enumerate(('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'))}}
            for d in ids[week_id].findall(q('Day')):
                typ = d.get('dayType')
                if typ not in aliases:
                    raise ValueError(f'{week_id}: unsupported dayType {typ}')
                values = day(d.get('dayScheduleIdRef'))
                for i in aliases[typ]:
                    if i in result and result[i] != values:
                        raise ValueError(f'{week_id}: overlapping weekday definitions')
                    result[i] = values
            if set(result) != set(range(7)):
                raise ValueError(f'{week_id}: incomplete weekly schedule')
            return result
        hourly = [None]*8760
        for year in node.findall(q('YearSchedule')):
            begin = year.findtext(q('BeginDate')); end = year.findtext(q('EndDate'))
            import datetime
            def ordinal(value):
                date = datetime.date.fromisoformat(value)
                return datetime.date(2001,date.month,date.day).timetuple().tm_yday-1
            start, stop = ordinal(begin), ordinal(end)
            week_node = year.find(q('WeekScheduleId'))
            weekly = week(week_node.get('weekScheduleIdRef') or week_node.text)
            days = list(range(start, stop+1)) if start <= stop else list(range(start,365))+list(range(stop+1))
            for d in days:
                if hourly[d*24] is not None:
                    raise ValueError(f'{ident}: overlapping YearSchedule ranges')
                hourly[d*24:(d+1)*24] = weekly[d%7]
        if any(v is None for v in hourly):
            raise ValueError(f'{ident}: incomplete yearly schedule')
        schedules[ident] = hourly
        return hourly

    for node in root.iter(q('Surface')):
        owners = [node.get('id'), *[o.get('id') for o in node.findall(q('Opening'))]]
        for owner in owners:
            entity = ids[owner]; settings = {}
            if entity.get('openingType') in {'SlidingDoor','NonSlidingDoor'}:
                settings['opaque_opening'] = True
            ref = entity.get('constructionIdRef') or entity.get('windowTypeIdRef')
            resource = ids.get(ref)
            if ref and resource is None:
                issue(owner, 'construction', f'missing construction/window resource {ref}')
            if resource is not None:
                scalar(resource, 'U-value', ref, settings, 'u_value', unit='WPerSquareMeterK')
                if 'u_value' in settings:
                    settings['u_convention'] = 'air_to_air'
                layers = []
                material_optics = []
                try:
                    for link in resource.findall(q('LayerId')):
                        layer = ids[link.get('layerIdRef')]
                        links = layer.findall(q('MaterialId'))
                        if len(links) != 1 or links[0].get('percentOfLayer', '100') not in {'100', '100.0'}:
                            raise ValueError('parallel/mixed layer materials need an explicit equivalent construction')
                        mat = ids[links[0].get('materialIdRef')]
                        optical_values = {}
                        for child in mat:
                            tag = child.tag.rsplit('}',1)[-1]
                            typ = child.get('type','').lower()
                            field = None
                            if tag in {'Absorptance','Reflectance'} and typ in {'solar','extsolar','exteriorsolar'}:
                                field = 'solar_reflectance'
                            if tag == 'Emittance' and typ in {'','extir','exterior','outside'}:
                                field = 'outside_emissivity'
                            if tag == 'Emittance' and typ in {'intir','interior','inside'}:
                                field = 'inside_emissivity'
                            if field and child.text:
                                value = numeric(child)
                                if tag == 'Absorptance':
                                    value = 1-value
                                optical_values[field] = record(mat.get('id'),field,value,child,
                                    f"//*[@id='{mat.get('id')}']/{tag}[@type='{child.get('type','')}']")
                        material_optics.append(optical_values)
                        props = {}
                        for xml, key, default in [('Thickness','thickness_m','Meters'), ('Conductivity','conductivity_w_mk','WPerMeterK'),
                                                  ('Density','density_kg_m3','KgPerCubicM'), ('SpecificHeat','specific_heat_j_kgk','JPerKgK')]:
                            scalar(mat, xml, mat.get('id'), props, key, unit=default)
                        if set(props) == {'thickness_m','conductivity_w_mk','density_kg_m3','specific_heat_j_kgk'}:
                            layers.append(props)
                        else:
                            raise ValueError(f'{mat.get("id")}: incomplete physical layer properties')
                    if layers:
                        # gbXML lists exterior -> interior. Full-element layers
                        # in MoosasPy follow the source normal (back -> front).
                        settings.update(layers=list(reversed(layers)), construction_scope='full_element')
                        settings.update({k:v for k,v in material_optics[0].items() if k != 'inside_emissivity'})
                        inside = material_optics[-1].get('inside_emissivity',material_optics[-1].get('outside_emissivity'))
                        if inside is not None:
                            settings['inside_emissivity'] = inside
                except (KeyError, ValueError) as exc:
                    issue(owner, 'layers', str(exc))
                for tag, key in [('SolarTransmittance','solar_transmittance'), ('SolarReflectance','solar_reflectance'),
                                 ('Emittance','outside_emissivity'), ('InsideEmittance','inside_emissivity')]:
                    scalar(resource, tag, ref, settings, key)
                for child in resource.findall(q('Emittance')):
                    if child.get('type','').lower() in {'intir','interior','inside'}:
                        settings['inside_emissivity'] = record(ref,'inside_emissivity',numeric(child),child,f"//*[@id='{ref}']/Emittance[@type='{child.get('type')}']")
                # gbXML often represents optical values as typed Transmittance/
                # Reflectance records. SHGC remains separate from transmittance.
                for tag, key in [('Transmittance','solar_transmittance'), ('Reflectance','solar_reflectance')]:
                    for optical in resource.findall(q(tag)):
                        if optical.get('type', '').lower() in {'solar','extsolar','exteriorsolar'}:
                            try:
                                settings[key] = record(ref,key,numeric(optical),optical,f"//*[@id='{ref}']/{tag}[@type='Solar']")
                            except ValueError as exc:
                                issue(owner,key,str(exc))
                shgc = resource.find(q('SolarHeatGainCoeff'))
                if shgc is not None:
                    settings['source_shgc'] = record(ref,'source_shgc',numeric(shgc),shgc,f"//*[@id='{ref}']/SolarHeatGainCoeff")
                    settings['shgc'] = settings['source_shgc']  # semantic/legacy readers; never used as tau
                    if 'solar_transmittance' not in settings:
                        issue(owner, 'solar_transmittance', 'source SHGC is not solar transmittance; explicit glazing optics are required')
                if resource.tag == q('WindowType') and any(resource.findall(q(t)) for t in ('Glaze','Gap')):
                    issue(owner, 'glazing', 'detailed multilayer glazing requires an equivalent massless optical/thermal model')
            bc = entity.findtext(q('OutsideBoundaryCondition'))
            typ = entity.get('surfaceType','')
            if typ in {'UndergroundSlab','SlabOnGrade','UndergroundWall'} or bc in {'Ground','GroundFCfactorMethod'}:
                settings['boundary_condition'] = 'ground'
            elif bc in {'Adiabatic','Surface'} or len(node.findall(q('AdjacentSpaceId'))) > 1:
                settings['boundary_condition'] = 'adiabatic'
            elif typ in {'ExteriorWall','Roof','RaisedFloor','Shade'} or bc == 'Outdoors':
                settings['boundary_condition'] = 'outdoors'
            if 'u_value' in settings:
                if settings['u_value'] <= 0:
                    issue(owner,'u_value','source U must be positive')
                elif settings.get('layers'):
                    r = sum(l['thickness_m']/l['conductivity_w_mk'] for l in settings['layers'])
                    rsi = .17 if typ in {'Floor','SlabOnGrade','UndergroundSlab','RaisedFloor'} else .10 if typ in {'Roof','Ceiling'} else .13
                    derived = 1/(r+rsi+.04)
                    if abs(settings['u_value']/derived-1) > .05:
                        issue(owner,'u_value/layers','source U and physical layers disagree by more than 5% under declared reference films')
            surfaces[owner] = settings

    for node in root.iter(q('Space')):
        owner = node.get('id'); settings = {}
        for tag, key, factor in [('Area','floor_area_m2',area_scale), ('Volume','volume_m3',volume_scale),
                                  ('HeatingSetpointTemp','zone_h_temp',1.), ('CoolingSetpointTemp','zone_c_temp',1.),
                                  ('LightPowerPerArea','zone_lighting',1.), ('LightingPowerPerArea','zone_lighting',1.),
                                  ('EquipPowerPerArea','zone_equipment',1.), ('EquipmentPowerPerArea','zone_equipment',1.),
                                  ('InfiltrationFlow','zone_infiltration',1.), ('OutdoorAirFlowPerPerson','zone_pfav',1.)]:
            default = 'AirChangesPerHour' if tag == 'InfiltrationFlow' else 'C' if 'SetpointTemp' in tag else None
            scalar(node,tag,owner,settings,key,unit=default,factor=factor)
        people = node.find(q('PeopleNumber'))
        if people is not None and people.text:
            value = float(people.text); unit = people.get('unit','NumberOfPeople')
            if unit == 'NumberOfPeople':
                if not settings.get('floor_area_m2',0)>0:
                    issue(owner,'zone_ppsm','NumberOfPeople needs a positive source floor area')
                    value = 0.
                else:
                    value /= settings['floor_area_m2']
            elif unit == 'SquareMetersPerPerson':
                value = 1/value
            elif unit == 'SquareFeetPerPerson':
                value = 1/(value*AREA['SquareFeet'])
            elif unit != 'PeoplePerSquareMeter':
                issue(owner,'zone_ppsm',f'unsupported people unit {unit}')
            settings['zone_ppsm'] = record(owner,'zone_ppsm',value,people,f"//*[@id='{owner}']/PeopleNumber")
        for heat in node.findall(q('PeopleHeatGain')):
            if heat.get('heatGainType','Sensible') == 'Sensible':
                try:
                    settings['zone_popheat'] = record(owner,'zone_popheat',numeric(heat,unit='WPerPerson'),heat,f"//*[@id='{owner}']/PeopleHeatGain[@heatGainType='Sensible']")
                except ValueError as exc:
                    issue(owner,'zone_popheat',str(exc))
        infiltration = node.find(q('InfiltrationFlow'))
        if infiltration is not None and infiltration.get('unit','AirChangesPerHour') != 'AirChangesPerHour' and 'zone_infiltration' in settings:
            if settings.get('volume_m3',0)>0:
                settings['zone_infiltration'] /= settings['volume_m3']
                for r in records:
                    if r['source_id']==owner and r['parameter']=='zone_infiltration':
                        r['value_si']=settings['zone_infiltration']; r['conversion']='source volume flow / source volume -> ACH'
            else:
                issue(owner,'zone_infiltration','volume-flow infiltration needs positive source volume')
        zone_ref = node.get('zoneIdRef')
        if zone_ref:
            source_zone = ids.get(zone_ref)
            if source_zone is None:
                issue(owner,'zoneIdRef',f'missing source zone {zone_ref}')
            else:
                for tag,key in [('HeatingSetpointTemp','zone_h_temp'),('CoolingSetpointTemp','zone_c_temp'),
                                ('HeatingEfficiency','zone_HeatingEER'),('CoolingEfficiency','zone_collingEER')]:
                    if key not in settings:
                        scalar(source_zone,tag,zone_ref,settings,key,unit='C' if 'Temp' in tag else '')
                if any(source_zone.get(k) for k in ('airLoopIdRef','hydronicLoopIdRef')):
                    issue(owner,'HVAC','detailed source HVAC loops are not equivalent to ideal constant-COP zones')
        for attr, key, multiplier in [('peopleScheduleIdRef','zone_ppsm',True), ('lightScheduleIdRef','zone_lighting',True),
                                      ('equipmentScheduleIdRef','zone_equipment',True), ('heatScheduleIdRef','zone_h_temp',False),
                                      ('coolScheduleIdRef','zone_c_temp',False), ('infiltrationScheduleIdRef','zone_infiltration',True)]:
            ref = node.get(attr)
            if ref:
                try:
                    values = schedule(ref)
                    if multiplier and key not in settings:
                        raise ValueError(f'{attr} lacks its source design value')
                    name = f'GBXML_{owner}_{key}'
                    schedules[name] = [v*settings[key] for v in values] if multiplier else values
                    settings[key] = name
                    records.append(dict(source_id=owner,parameter=key,schedule=name,source_schedule_id=ref,xpath=f"//*[@id='{owner}']/@{attr}",source='gbXML'))
                except (KeyError,ValueError,TypeError) as exc:
                    issue(owner,key,str(exc))
        condition = node.get('conditionType','HeatedAndCooled')
        settings['hvac_enabled'] = 0. if condition in {'Unconditioned','Unheated'} else 1.
        if condition in {'Heated','Cooled'}:
            issue(owner,'conditionType',f'{condition} requires separate heating/cooling availability support')
        zones[owner] = settings
    azimuth = root.find('.//'+q('CADModelAzimuth'))
    if azimuth is not None and azimuth.text:
        energy_defaults['north_angle_degrees'] = record('gbXML-document','north_angle_degrees',float(azimuth.text),azimuth,'//Location/CADModelAzimuth')
    required_surface_ids = [n.get('id') for node in root.iter(q('Surface')) if node.findall(q('AdjacentSpaceId'))
                            for n in [node,*node.findall(q('Opening'))]]
    return dict(schema='moosas.external-thermal-model', version=1, source_format='gbXML',
                required_surface_ids=required_surface_ids,
                source_name=source_name, source_sha256=source_sha256 or hashlib.sha256(raw.encode()).hexdigest(),
                energy_defaults=energy_defaults,
                surface_settings=surfaces, zone_settings=zones, schedules=schedules,
                records=records, unresolved=unresolved,
                precedence='External supplied values are authoritative; defaults fill missing fields only.')


def capture(root, graph, source_path):
    source = Path(source_path)
    evidence = project(root,source_name=source.name,source_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    graph.bind('thermalEvidence', GBE)
    graph.set((DOC,RDF.type,GBE.ExternalThermalModel))
    graph.set((DOC,GBE.projection,Literal(json.dumps(evidence,ensure_ascii=False,allow_nan=False),datatype=URIRef('urn:moosas:json'))))
    graph.set((DOC,GBE.sha256,Literal(evidence['source_sha256'])))
    for i, record in enumerate(evidence['records']):
        entity = URIRef(f'{DOC}/field/{i}')
        graph.add((DOC,GBE.field,entity))
        for key in ('source_id','parameter','xpath','original_value','original_unit'):
            if key in record:
                graph.add((entity,GBE[key],Literal(record[key])))
        if 'value_si' in record:
            graph.add((entity,GBE.valueSI,Literal(record['value_si'],datatype=XSD.double)))
    return evidence


def attach(model, evidence):
    if not evidence:
        return
    model.energy_settings['external_thermal_model'] = deepcopy(evidence)
    model.energy_settings['energy_defaults'] = {**model.energy_settings.get('energy_defaults',{}),**evidence.get('energy_defaults',{})}
    for name, values in evidence['schedules'].items():
        model.schedule[name] = dict(type='Hourly',value=values)
    all_spaces = list(model.spaceList)+list(model.voidList)
    for space in all_spaces:
        settings = evidence['zone_settings'].get(str(space.id),{})
        for key,value in settings.items():
            if key in space.explicit_settings and key in space.settings and space.settings[key] != value:
                raise ValueError(f'gbXML source mismatch: zone {space.id}/{key}')
        space.settings.update(settings)
        space.explicit_settings = sorted(set(space.explicit_settings)|set(settings))
        space.conditioned = bool(settings.get('hvac_enabled',space.conditioned))
        for element in space.getAllFaces(to_dict=False):
            values = evidence['surface_settings'].get(str(element.Uid),{})
            for key,value in values.items():
                if key in element.explicit_settings and key in element.settings and element.settings[key] != value:
                    raise ValueError(f'gbXML source mismatch: surface {element.Uid}/{key}')
            element.settings.update(deepcopy(values))
            element.settings['_gbxml_source_sha256'] = evidence['source_sha256']
            element.settings['_gbxml_fields'] = sorted(values)
            element.explicit_settings.update(values)
    # Source spaces with declared area/volume remain energy zones even if a
    # geometry repair classified an irregular ring as a void.
    promoted = [s for s in model.voidList if str(s.id) in evidence['zone_settings'] and
                evidence['zone_settings'][str(s.id)].get('floor_area_m2',0)>0 and
                evidence['zone_settings'][str(s.id)].get('volume_m3',0)>0]
    model.spaceList.extend(promoted)
    model.voidList = [s for s in model.voidList if s not in promoted]


def from_graph(model, graph):
    payload = graph.value(DOC,GBE.projection)
    evidence = json.loads(str(payload)) if payload is not None else model.energy_settings.get('external_thermal_model')
    attach(model,evidence)


def encode(model, graph):
    evidence = model.energy_settings.get('external_thermal_model')
    if evidence:
        graph.set((DOC,RDF.type,GBE.ExternalThermalModel))
        graph.set((DOC,GBE.sha256,Literal(evidence['source_sha256'])))
        graph.set((DOC,GBE.projection,Literal(json.dumps(evidence,ensure_ascii=False,allow_nan=False),datatype=URIRef('urn:moosas:json'))))


def validate(model, surface_overrides, zone_overrides, template_overrides_model=False):
    evidence = model.energy_settings.get('external_thermal_model')
    if not evidence:
        return
    if evidence.get('schema') != 'moosas.external-thermal-model' or evidence.get('source_format') != 'gbXML':
        raise ValueError('Unsupported external thermal evidence schema/format; source settings must be mapped before simulation')
    if evidence.get('unresolved'):
        issue = evidence['unresolved'][0]
        raise ValueError(f"gbXML thermal field unresolved: {issue['source_id']}/{issue['field']}: {issue['reason']}")
    if template_overrides_model:
        raise ValueError('gbXML source alignment forbids template_overrides_model; create a separate source scenario')
    for key,value in evidence.get('energy_defaults',{}).items():
        if model.energy_settings.get('energy_defaults',{}).get(key) != value:
            raise ValueError(f'gbXML source mismatch: EnergyDefaults/{key}')
    missing = set(evidence['zone_settings'])-{str(s.id) for s in model.spaceList}
    if missing:
        raise ValueError(f'gbXML source zones missing from energy model: {sorted(missing)}')
    known = {str(e.Uid) for space in model.spaceList for e in space.getAllFaces(to_dict=False)}
    missing_surfaces = set(evidence.get('required_surface_ids',()))-known
    if missing_surfaces:
        raise ValueError(f'gbXML source surfaces missing from energy model: {sorted(missing_surfaces)}')
    for name, values in evidence['schedules'].items():
        actual = model.schedule.get(name,{})
        if actual.get('type') != 'Hourly' or list(map(float,actual.get('value',()))) != values:
            raise ValueError(f'gbXML source schedule mismatch: {name}')
    for zone in list(model.spaceList)+list(model.voidList):
        expected = evidence['zone_settings'].get(str(zone.id),{})
        actual = {**zone.settings,**zone_overrides.get(str(zone.id),{})}
        for key,value in expected.items():
            if actual.get(key) != value:
                raise ValueError(f'gbXML source mismatch: zone {zone.id}/{key}')
        for element in zone.getAllFaces(to_dict=False):
            expected = evidence['surface_settings'].get(str(element.Uid),{})
            actual = {**element.settings,**surface_overrides.get(str(element.Uid),{})}
            actual.update(actual.get('side_overrides',{}).get(str(zone.id),{}))
            for side, patch in surface_overrides.items():
                if side.startswith(f'{zone.id}/{element.Uid}/'):
                    for key,value in expected.items():
                        if key in patch and patch[key] != value:
                            raise ValueError(f'gbXML source mismatch: {side}/{key}')
            for key,value in expected.items():
                if actual.get(key) != value:
                    raise ValueError(f'gbXML source mismatch: surface {element.Uid}/{key}')
            # Changing representation can otherwise bypass an authoritative U
            # or layer record through the input builder's precedence rules.
            if 'layers' in expected and actual.get('construction_scope','full_element') != 'full_element':
                raise ValueError(f'gbXML source mismatch: {element.Uid}/construction_scope')
            if 'layers' not in expected and 'u_value' in expected and any(k in actual for k in ('layers','resistance_m2k_w')):
                raise ValueError(f'gbXML source U bypassed by alternate construction: {element.Uid}')
