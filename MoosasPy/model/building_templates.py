"""Building-template catalogs and their runtime adapters."""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import numpy as np


DEFAULT_LIBRARY = Path(__file__).resolve().parents[1] / 'db' / 'template'


def _read_catalog_manifest(filename):
    data = json.loads(filename.read_text(encoding='utf-8'))
    if data.get('schema') != 'moosas.building-template-library':
        raise ValueError(f'Unsupported building template catalog schema in {filename.name}')
    return data


def load_building_template_library(library_path=None):
    """Load the bundled default catalog or an explicitly provided local catalog."""
    folder = DEFAULT_LIBRARY if library_path is None else Path(library_path)
    manifest = _read_catalog_manifest(folder / 'library.json')
    library = dict(schema=manifest['schema'], standards={})
    countries = manifest.get('countries')
    if not isinstance(countries, dict) or not countries:
        raise ValueError('Catalog countries are required')
    for country, country_data in countries.items():
        if (not isinstance(country, str) or not country or Path(country).name != country
                or country in {'.', '..'} or ':' in country or not isinstance(country_data, dict)):
            raise ValueError(f'Invalid country package {country!r}')
        standards = country_data.get('standards')
        if not isinstance(standards, dict) or not standards:
            raise ValueError(f'{country}: catalog standards are required')
        for standard_id, registration in standards.items():
            namespace = f'{country}/{standard_id}'
            if (not isinstance(standard_id, str) or not standard_id
                    or Path(standard_id).name != standard_id or standard_id in {'.', '..'}
                    or ':' in standard_id or not isinstance(registration, dict)):
                raise ValueError(f'Invalid template package path {namespace!r}')
            metadata = registration.get('standard_metadata')
            assumptions = registration.get('template_assumptions')
            operational = registration.get('operational_metadata')
            if (not isinstance(metadata, dict) or not isinstance(metadata.get('source_url'), str)
                    or not metadata['source_url'] or {'country', 'id'} & metadata.keys()
                    or not isinstance(assumptions, dict) or not isinstance(operational, dict)):
                raise ValueError(f'{namespace}: catalog metadata is required')
            standard = {'country': country, 'id': standard_id, **metadata}
            package = folder / country / standard_id
            files = sorted(package.glob('*.json'))
            if not files:
                raise ValueError(f'{namespace}: no direct building templates found')
            templates = {}
            schedules = {}
            for filename in files:
                template_id, record = _read_direct_template(
                    filename, package, standard, assumptions, operational, schedules)
                if template_id in templates:
                    raise ValueError(f'Duplicate template id {template_id!r}')
                templates[template_id] = record
            library['standards'][namespace] = {'templates': templates}
    return library


def _read_direct_template(filename, package, standard, assumptions, operational, schedules):
    source = json.loads(filename.read_text(encoding='utf-8'))
    if not isinstance(source, dict) or set(source) != {'BuildingTemplate'}:
        raise ValueError(f'{filename.name}: direct building template requires only BuildingTemplate')
    original = source['BuildingTemplate']
    if not isinstance(original, dict) or set(original) != {'metadata', 'Space', 'Envelope'}:
        raise ValueError(f'{filename.name}: invalid direct BuildingTemplate fields')
    metadata = original['metadata']
    if (not isinstance(metadata, dict)
            or metadata.get('schema') != 'moosas.building-template'
            or metadata.get('country') != standard.get('country')
            or metadata.get('standard_name') != standard.get('id')
            or metadata.get('standard_source') != standard.get('source_url')
            or not isinstance(metadata.get('building_type'), str)
            or not isinstance(metadata.get('climate_zone'), str)):
        raise ValueError(f'{filename.name}: standard metadata does not match the registered package')
    expected_file = f"{metadata.get('building_type')}_{metadata.get('climate_zone')}.json"
    if filename.name != expected_file:
        raise ValueError(f'{filename.name}: filename does not match building type and climate zone')
    if metadata.get('id') != (
            f"{metadata['country']}_{metadata['standard_name']}_"
            f"{metadata['building_type']}_{metadata['climate_zone']}"):
        raise ValueError(f'{filename.name}: invalid template metadata id')
    namespace = f"{standard['country']}/{standard['id']}"
    template_id = f"{namespace}/{metadata['climate_zone']}/{metadata['building_type']}"
    resolved = _load_schedule_sources(original, package, template_id, schedules=schedules)
    profile = {
        'schema': 'moosas.building-template',
        'id': template_id,
        'standard': deepcopy(standard),
        'BuildingTemplate': {'Space': resolved['Space'], 'Envelope': resolved['Envelope']},
        'metadata': {
            'standard_source': deepcopy(standard),
            'template_metadata': deepcopy(metadata),
            'energy_assumptions': deepcopy(assumptions),
            'operational_metadata': deepcopy(operational),
        },
    }
    _validate_template_snapshot(profile)
    return template_id, profile


def _nullable_schedule(label):
    return label.endswith('/heating_setpoint') or label.endswith('/cooling_setpoint')


def _validate_value_source(value, label, *, resolved=False):
    if not isinstance(value, dict):
        raise ValueError(f'{label}: parameters must use constant or schedule value sources')
    mode = value.get('mode')
    if mode == 'constant':
        if set(value) != {'mode', 'value', 'unit'}:
            raise ValueError(f'{label}: constant values require mode, value, and unit')
        numeric = value['value']
        if (isinstance(numeric, bool) or not isinstance(numeric, (int, float)) or not math.isfinite(numeric)
                or not isinstance(value['unit'], str) or not value['unit']):
            raise ValueError(f'{label}: constant value must be finite and have a unit')
    elif mode == 'schedule':
        expected = {'mode', 'file', 'name', 'unit'}
        if resolved:
            expected.add('values')
        if set(value) != expected:
            raise ValueError(f'{label}: schedule values require mode, file, name, and unit')
        if (not isinstance(value['file'], str) or not value['file'].lower().endswith('.sch')
                or Path(value['file']).is_absolute()
                or any(part in {'..', ''} for part in Path(value['file']).parts)
                or not isinstance(value['name'], str) or not value['name']
                or not isinstance(value['unit'], str) or not value['unit']):
            raise ValueError(f'{label}: invalid relative .sch schedule reference')
        if resolved and (not isinstance(value['values'], list) or len(value['values']) != 8760
                         or any(v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))
                                                   or not math.isfinite(v)) for v in value['values'])
                         or (not _nullable_schedule(label) and any(v is None for v in value['values']))):
            raise ValueError(f'{label}: resolved schedule must contain exactly 8760 finite values')
    else:
        raise ValueError(f'{label}: mode must be constant or schedule')


def _validate_parameter_sources(parameters, label, *, resolved=False):
    if not isinstance(parameters, dict):
        raise ValueError(f'{label}: parameters must be an object')
    for name, value in parameters.items():
        if not name or not isinstance(name, str):
            raise ValueError(f'{label}: parameter names must be nonempty strings')
        _validate_value_source(value, f'{label}/{name}', resolved=resolved)


def _load_schedule_sources(value, package_path, label, *, schedules=None):
    schedules = {} if schedules is None else schedules
    if isinstance(value, dict) and value.get('mode') in {'constant', 'schedule'}:
        _validate_value_source(value, label)
        if value['mode'] == 'constant':
            return deepcopy(value)
        schedule_file = Path(value['file'])
        resolved = (package_path / schedule_file).resolve()
        if package_path.resolve() not in resolved.parents:
            raise ValueError(f'{label}: schedule path escapes its template package')
        if resolved not in schedules:
            document = json.loads(resolved.read_text(encoding='utf-8'))
            if (not isinstance(document, dict)
                    or document.get('schema') != 'moosas.schedule'
                    or 'schedules' not in document):
                raise ValueError(f'{label}: unsupported .sch schema')
            document_schedules = document.get('schedules')
            if not isinstance(document_schedules, dict):
                raise ValueError(f'{label}: .sch schedules must be an object')
            for name, entry in document_schedules.items():
                if not isinstance(name, str) or not name:
                    raise ValueError(f'{label}: schedule names must be nonempty strings')
                nullable = name.endswith(('_heating_setpoint', '_cooling_setpoint'))
                values = entry.get('values') if isinstance(entry, dict) else None
                if (not isinstance(entry, dict) or set(entry) != {'unit', 'values'}
                        or not isinstance(entry['unit'], str)
                        or not entry['unit'] or not isinstance(values, list) or len(values) != 8760
                        or any(value is not None and (isinstance(value, bool)
                               or not isinstance(value, (int, float)) or not math.isfinite(value))
                               for value in values)
                        or (not nullable and any(value is None for value in values))):
                    raise ValueError(f'{label}: invalid schedule entry {name!r}')
            schedules[resolved] = document_schedules
        document_schedules = schedules[resolved]
        if value['name'] not in document_schedules:
            raise ValueError(f'{label}: schedule {value["name"]!r} is missing from {value["file"]}')
        item = document_schedules[value['name']]
        if not isinstance(item, dict) or set(item) != {'unit', 'values'}:
            raise ValueError(f'{label}: schedule entry must be an object')
        values = item.get('values')
        if item.get('unit') != value['unit']:
            raise ValueError(f'{label}: schedule unit does not match template parameter unit')
        if (not isinstance(values, list) or len(values) != 8760 or any(
                v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v))
                for v in values) or (not _nullable_schedule(label) and any(v is None for v in values))):
            raise ValueError(f'{label}: schedule must contain exactly 8760 finite values')
        return {**deepcopy(value), 'values': deepcopy(values)}
    if isinstance(value, dict):
        return {key: _load_schedule_sources(item, package_path, f'{label}/{key}', schedules=schedules)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_load_schedule_sources(item, package_path, f'{label}/{index}', schedules=schedules)
                for index, item in enumerate(value)]
    return deepcopy(value)

def configure_model_resources(model, template_path=None):
    """List available templates on a model without applying any of them."""
    if template_path is not None or not model.buildingTemplate:
        model.buildingTemplate = building_templates(template_path)
    return model


def building_templates(library_path=None):
    library = load_building_template_library(library_path)
    result = {}
    for package in library['standards'].values():
        result.update(package['templates'])
    return result


def validate_template_snapshot(settings):
    """Reject obsolete per-space snapshots; templates now belong to the model."""
    if '_thermal_template' in settings or '_building_template' in settings:
        raise ValueError('Per-space template snapshots are unsupported; reapply the building template explicitly')
    if {'zone_heating_availability', 'zone_cooling_availability'} & settings.keys():
        raise ValueError('Separate heating/cooling availability is unsupported; use hvac_enabled and setpoints')


def _validate_template_snapshot(profile):
    if (not isinstance(profile, dict) or profile.get('schema') != 'moosas.building-template'
            or not isinstance(profile.get('id'), str)):
        raise ValueError('A BuildingTemplate record with a valid id is required')
    template = profile.get('BuildingTemplate', {})
    if not isinstance(template, dict) or set(template) != {'Space', 'Envelope'}:
        raise ValueError('Direct BuildingTemplate snapshot requires Space and Envelope domains')
    space = template['Space']
    if not isinstance(space, dict) or not space:
        raise ValueError('Direct BuildingTemplate Space must contain room groups')
    required = {
        'air_exchange': {'infiltration'},
        'internal_gains': {'occupancy_density', 'equipment_power_density', 'lighting_power_density',
                           'occupancy_fraction', 'equipment_usage', 'lighting_usage'},
        'system': {'hvac_availability'},
    }
    for room, values in space.items():
        if not isinstance(room, str) or not room or not isinstance(values, dict) or set(values) != set(required):
            raise ValueError(f'Space/{room}: invalid room domains')
        for domain, names in required.items():
            parameters = values[domain]
            if not isinstance(parameters, dict) or names - set(parameters):
                raise ValueError(f'Space/{room}/{domain}: missing required parameters')
            if domain == 'internal_gains' and 'occupant_heat' in parameters:
                raise ValueError(f'Space/{room}/internal_gains/occupant_heat is not a template parameter')
            if domain == 'system' and {'heating_availability', 'cooling_availability'} & parameters.keys():
                raise ValueError(f'Space/{room}/system: separate heating/cooling availability is unsupported')
            _validate_parameter_sources(parameters, f'Space/{room}/{domain}', resolved=True)
    envelope = template['Envelope']
    expected_groups = {
        'exterior_wall', 'interior_wall', 'floor', 'ground', 'roof', 'window', 'skylight'}
    if not isinstance(envelope, dict) or set(envelope) != expected_groups:
        raise ValueError('Direct BuildingTemplate snapshot has invalid Envelope construction groups')
    allowed_parameters = {
        'u', 'shgc', 'visible_reflectance', 'visible_transmittance',
        'air_leakage_per_area', 'opening_ratio', 'r'}
    for construction, parameters in envelope.items():
        if not isinstance(parameters, dict) or set(parameters) - allowed_parameters:
            raise ValueError(f'Envelope/{construction}: invalid direct construction properties')
        _validate_parameter_sources(parameters, f'Envelope/{construction}', resolved=True)
    metadata = profile.get('metadata')
    standard = profile.get('standard')
    if not isinstance(metadata, dict) or not isinstance(standard, dict) or metadata.get('standard_source') != standard:
        raise ValueError('Direct BuildingTemplate snapshot is missing standard provenance')
    source_metadata = metadata.get('template_metadata')
    if (not isinstance(source_metadata, dict)
            or source_metadata.get('schema') != 'moosas.building-template'
            or source_metadata.get('country') != standard.get('country')
            or source_metadata.get('standard_name') != standard.get('id')
            or source_metadata.get('standard_source') != standard.get('source_url')
            or profile['id'] != (
                f"{standard['country']}/{standard['id']}/"
                f"{source_metadata.get('climate_zone')}/{source_metadata.get('building_type')}")):
        raise ValueError('Direct BuildingTemplate snapshot id does not match its source metadata')
    if not isinstance(metadata.get('energy_assumptions'), dict) or not isinstance(metadata.get('operational_metadata'), dict):
        raise ValueError('BuildingTemplate snapshot is missing catalog assumptions')


def space_template(model, space, template_id=None, *, templates=None):
    """Resolve and validate the space's model-owned template."""
    validate_template_snapshot(space.settings)
    name = template_id if template_id is not None else space.settings.get('zone_template')
    if not name:
        raise ValueError(f'{space.id}: explicitly select a BuildingTemplate before simulation')
    records = model.buildingTemplate if templates is None else templates
    if name not in records:
        raise ValueError(f'{space.id}: unknown building template {name!r}; use a complete namespaced id')
    profile = records[name]
    _validate_template_snapshot(profile)
    if profile['id'] != name:
        raise ValueError('BuildingTemplate id does not match the selected template')
    _space_room(space, profile)
    return profile


def apply_building_template(model, template_id, *, template_path=None, zone_ids=None,
                            preserve_explicit=True):
    """Select JSON templates explicitly. Geometry and source files are unchanged.

    ``preserve_explicit=False`` rebases zone operating values only. Element
    constructions remain authoritative; use surface overrides to replace them.
    """
    records = building_templates(template_path)
    if template_id not in records:
        raise ValueError(f'Unknown building template {template_id!r}')
    record = records[template_id]
    _validate_template_snapshot(record)
    selected = set(map(str, zone_ids)) if zone_ids is not None else {str(s.id) for s in model.spaceList}
    if selected - {str(s.id) for s in model.spaceList}:
        raise ValueError('Unknown zone_ids in template application')
    spaces = [space for space in model.spaceList if str(space.id) in selected]
    for space in spaces:
        validate_template_snapshot(space.settings)
        _space_room(space, record)
    model.buildingTemplate[template_id] = record
    for space in spaces:
        if not preserve_explicit:
            replaced = set(SPACE_PARAMETERS)
            space.explicit_settings = [k for k in space.explicit_settings if k not in replaced]
            for key in replaced:
                space.settings.pop(key, None)
        space.settings['zone_template'] = template_id
    return model


SPACE_PARAMETERS = {
    'zone_pfav': ('air_exchange', 'fresh_air'),
    'zone_infiltration': ('air_exchange', 'infiltration'),
    'zone_ppsm': ('internal_gains', 'occupancy_density'),
    'zone_lighting': ('internal_gains', 'lighting_power_density'),
    'zone_equipment': ('internal_gains', 'equipment_power_density'),
    'zone_h_temp': ('system', 'heating_setpoint'),
    'zone_c_temp': ('system', 'cooling_setpoint'),
    'zone_collingEER': ('system', 'cooling_cop'),
    'zone_HeatingEER': ('system', 'heating_cop'),
    'hvac_enabled': ('system', 'hvac_availability'),
}


def _space_room(space, profile):
    rooms = profile['BuildingTemplate']['Space']
    room = space.settings.get('zone_room')
    if room is None and set(rooms) == {'default'}:
        room = 'default'
    if room not in rooms:
        raise ValueError(f'{space.id}: set zone_room to one of {sorted(rooms)}')
    return room


def _source_values(source):
    return source['value'] if source['mode'] == 'constant' else source['values']


def _room_source(profile, room, domain, name):
    source = profile['BuildingTemplate']['Space'][room][domain].get(name)
    if source is None:
        return None
    return _source_values(source)


def _multiply_values(*values):
    if all(np.isscalar(value) for value in values):
        return math.prod(values)
    arrays = [np.full(8760, value, dtype=float) if np.isscalar(value)
              else np.asarray(value, dtype=float) for value in values]
    if any(array.shape != (8760,) for array in arrays):
        raise ValueError('Template schedules must contain exactly 8760 values')
    return np.prod(arrays, axis=0).tolist()


def template_parameter(profile, key, room='default'):
    """Resolve a room parameter into a model simulation setting."""
    room_values = profile['BuildingTemplate']['Space'][room]
    if key == 'zone_ppsm':
        return _multiply_values(
            _room_source(profile, room, 'internal_gains', 'occupancy_density'),
            _room_source(profile, room, 'internal_gains', 'occupancy_fraction'))
    if key == 'zone_equipment':
        return _multiply_values(
            _room_source(profile, room, 'internal_gains', 'equipment_power_density'),
            _room_source(profile, room, 'internal_gains', 'equipment_usage'))
    if key == 'zone_lighting':
        return _multiply_values(
            _room_source(profile, room, 'internal_gains', 'lighting_power_density'),
            _room_source(profile, room, 'internal_gains', 'lighting_usage'))
    if key == 'zone_pfav':
        air_exchange = room_values['air_exchange']
        fresh_air = _room_source(profile, room, 'air_exchange', 'fresh_air') if 'fresh_air' in air_exchange else 0.
        availability = (_room_source(profile, room, 'air_exchange', 'fresh_air_availability')
                        if 'fresh_air_availability' in air_exchange else 1.)
        return _multiply_values(fresh_air, availability)
    if key in {'zone_h_temp', 'zone_c_temp'}:
        domain, name = SPACE_PARAMETERS[key]
        value = _room_source(profile, room, domain, name)
        return value if value is not None else [None] * 8760
    if key == 'zone_collingEER':
        return _room_source(profile, room, 'system', 'cooling_cop') or 1.
    if key == 'zone_HeatingEER':
        return (_room_source(profile, room, 'system', 'heating_efficiency')
                or _room_source(profile, room, 'system', 'heating_cop') or 1.)
    domain, name = SPACE_PARAMETERS[key]
    return _room_source(profile, room, domain, name)


def readonly(values, *, allow_nan=False):
    result = np.array(values, dtype=float, copy=True)
    finite = np.isfinite(result)
    if result.ndim != 1 or not np.all(finite | (allow_nan & np.isnan(result))):
        raise ValueError('Hourly inputs must be finite one-dimensional arrays')
    result.setflags(write=False)
    return result


def expand_schedule(model, value, label, cache=None, *, allow_nan=False):
    """Resolve absolute model schedules to immutable 8760-hour inputs."""
    cache = {} if cache is None else cache
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            name = value
            if name in cache:
                if cache[name] is None:
                    raise ValueError(f'{label}: cyclic schedule {name}')
                return cache[name]
            if name not in model.schedule:
                raise ValueError(f'{label}: unknown schedule {name!r}')
            cache[name] = None
            item = model.schedule[name]
            mode = str(item['type']).lower()
            values = item['value']
            if mode == 'weekly':
                if len(values) != 7:
                    raise ValueError(f'{label}: weekly schedule requires 7 days')
                days = [expand_schedule(model, name, label, cache)[:24] for name in values]
                result = np.concatenate([days[i % 7] for i in range(365)])
            elif mode in {'daily', 'hourly'}:
                expected = 24 if mode == 'daily' else 8760
                if len(values) != expected:
                    raise ValueError(f'{label}: {mode} requires {expected} values')
                result = np.tile(np.asarray(values, dtype=float), 365) if mode == 'daily' else np.asarray(values, dtype=float)
            else:
                raise ValueError(f'{label}: unsupported schedule mode {mode}')
            if item.get('value_mode', 'absolute') != 'absolute':
                raise ValueError(f'{label}: schedule multipliers require explicit conversion to absolute values')
            result = readonly(result, allow_nan=allow_nan)
            cache[name] = result
            return result
    if np.isscalar(value):
        return readonly(np.full(8760, float(value)))
    result = readonly(value, allow_nan=allow_nan)
    if result.shape != (8760,):
        raise ValueError(f'{label}: requires 8760 hourly values')
    return result


def resolve_zone_settings(space, profile, overrides=None, *, template_overrides_model=False):
    """Resolve template values and explicit edits once at the simulation boundary."""
    validate_template_snapshot(space.settings)
    validate_template_snapshot(overrides or {})
    settings = dict(space.settings)
    settings.pop('zone_popheat', None)
    if overrides and 'zone_popheat' in overrides:
        raise ValueError('zone_popheat overrides are unsupported; simulations use 75 W/person')
    if overrides and overrides.get('zone_template', profile['id']) != profile['id']:
        raise ValueError('Select templates explicitly rather than through zone_overrides')
    settings['zone_template'] = profile['id']
    room = _space_room(space, profile)
    settings['zone_room'] = room
    explicit = set() if template_overrides_model else set(space.explicit_settings)
    for key in SPACE_PARAMETERS:
        if key in explicit and (key not in settings or settings[key] is None):
            raise ValueError(f'{space.id}/{key}: explicit parameter has no value')
        if key not in explicit:
            settings[key] = template_parameter(profile, key, room)
    general_availability = expand_schedule(
        None, _room_source(profile, room, 'system', 'hvac_availability'),
        f'{space.id}/hvac_availability')
    if not np.all(np.isin(general_availability, (0, 1))):
        raise ValueError(f'{space.id}/hvac_availability: requires 0 or 1')
    settings['zone_hvac_availability'] = general_availability.tolist()
    settings.update(overrides or {})
    for key in ('zone_h_temp', 'zone_c_temp', 'hvac_enabled'):
        if settings[key] is None:
            raise ValueError(f'{space.id}/{key}: explicit parameter has no value')
        values = expand_schedule(
            getattr(space, 'parent', None), settings[key], f'{space.id}/{key}',
            allow_nan=key != 'hvac_enabled')
        if key == 'hvac_enabled' and not np.all(np.isin(values, (0, 1))):
            raise ValueError(f'{space.id}/{key}: requires 0 or 1')
    return settings


def surface_template_parameters(profile, role, boundary):
    """Select the envelope group shared by energy and visible-light inputs."""
    if boundary == 'ground':
        construction = 'ground'
    elif role == 'window':
        construction = 'window'
    elif role == 'skylight':
        construction = 'skylight'
    elif role in {'roof', 'ceiling'}:
        construction = 'roof'
    elif role == 'floor':
        construction = 'floor'
    elif boundary == 'adiabatic':
        construction = 'interior_wall'
    else:
        construction = 'exterior_wall'
    return profile['BuildingTemplate']['Envelope'][construction]


def resolve_surface_template(profile, role, boundary):
    """Map the selected construction group's direct values to an Energy surface."""
    assumptions = profile['metadata']['energy_assumptions']
    parameters = surface_template_parameters(profile, role, boundary)
    construction = role
    glass = role in {'window', 'skylight'}
    out = dict(assumptions['glass' if glass else 'slab' if role in {'floor', 'roof', 'ceiling'} else 'wall'])
    out['_template_id'] = profile['id']
    notes = []
    if boundary == 'adiabatic':
        out['resistance_m2k_w'] = assumptions['internal_resistance_m2k_w']
    elif boundary == 'ground':
        source = parameters.get('r')
        if source is not None:
            if source['mode'] != 'constant':
                raise ValueError(f'Envelope/{construction}/r: the energy model does not support scheduled surface values')
            out['resistance_m2k_w'] = source['value']
        else:
            out['resistance_m2k_w'] = (
                assumptions['ground_base_resistance_m2k_w']
                + assumptions['unspecified_ground_insulation_r'])
            notes.append('Ground-contact construction has no direct R-value; an explicitly declared engineering resistance is used.')
    else:
        u_source = parameters.get('u')
        if u_source is not None:
            if u_source['mode'] != 'constant':
                raise ValueError(f'Envelope/{construction}/u: the energy model does not support scheduled surface values')
            out['u_value'] = u_source['value']
            out['u_convention'] = 'air_to_air'
        else:
            out['resistance_m2k_w'] = assumptions['unspecified_resistance_m2k_w']
            notes.append(f'{construction} has no direct U-value; an explicitly declared engineering resistance is used.')
        if glass:
            shgc_source = parameters.get('shgc')
            if shgc_source is not None:
                if shgc_source['mode'] != 'constant':
                    raise ValueError(f'Envelope/{construction}/shgc: the energy model does not support scheduled surface values')
                shgc = shgc_source['value']
                absorptance = min(assumptions['glass_absorptance'], shgc)
                out['solar_transmittance'] = max(0., shgc - absorptance)
                out['solar_reflectance'] = 1. - shgc
    out['_template_source'] = 'direct'
    return out, tuple(notes)


def merge_surface_template(element, current, resolved, *, overrides_model=False, protected=()):
    """Only replace inherited legacy defaults; preserve explicit constructions."""
    explicit = (set() if overrides_model else set(getattr(element, 'explicit_settings', ()))) | set(protected)
    # Physical groups must be overridden as groups, avoiding competing R/U/layers.
    if explicit & {'layers', 'resistance_m2k_w', 'u_value'}:
        for key in ('layers','resistance_m2k_w','u_value','u_convention'):
            resolved.pop(key, None)
        # Keep whichever physical representation the explicit model selected.
        active = (set(protected) & {'layers','resistance_m2k_w','u_value'}) or explicit
        if 'layers' in active:
            current = {k:v for k,v in current.items() if k not in {'u_value','resistance_m2k_w'}}
        elif 'resistance_m2k_w' in active:
            current = {k:v for k,v in current.items() if k not in {'layers','u_value'}}
        else:
            current = {k:v for k,v in current.items() if k not in {'layers','resistance_m2k_w'}}
    if explicit & {'thermal_mass_j_m2k','volumetric_heat_capacity','thickness'}:
        resolved.pop('thermal_mass_j_m2k', None)
        active_mass = (set(protected) & {'thermal_mass_j_m2k','volumetric_heat_capacity','thickness'}) or explicit
        if 'thermal_mass_j_m2k' not in active_mass:
            current = {k:v for k,v in current.items() if k!='thermal_mass_j_m2k'}
    result = {**current, **{k:v for k,v in resolved.items() if k not in explicit}}
    result['_template_fields'] = [k for k in resolved if k not in explicit and not k.startswith('_')]
    if not explicit & {'layers', 'resistance_m2k_w', 'u_value'}:
        # A resolved R takes precedence over inherited U/layers, and conversely.
        if 'resistance_m2k_w' in resolved:
            result.pop('layers', None)
            result.pop('u_value', None)
        elif 'u_value' in resolved:
            result.pop('layers', None)
            result.pop('resistance_m2k_w', None)
    return result
