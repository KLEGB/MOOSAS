"""Versioned building-template catalogs and their runtime adapters."""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path


DEFAULT_LIBRARY = Path(__file__).resolve().parents[1] / 'db' / 'template'


def _read_catalog_manifest(filename):
    data = json.loads(filename.read_text(encoding='utf-8'))
    if data.get('schema') != 'moosas.building-template-library' or data.get('version') != 4:
        raise ValueError(f'Unsupported building template catalog schema/version in {filename.name}')
    return data


def load_building_template_library(library_path=None):
    """Load the bundled default catalog or an explicitly provided local catalog."""
    folder = DEFAULT_LIBRARY if library_path is None else Path(library_path)
    manifest = _read_catalog_manifest(folder / 'library.json')
    library = dict(schema=manifest['schema'], version=manifest['version'], standards={})
    for namespace in manifest['standards']:
        if not isinstance(namespace, str):
            raise ValueError(f'Invalid template package path {namespace!r}')
        if not namespace or Path(namespace).name != namespace or namespace in {'.', '..'} or ':' in namespace:
            raise ValueError(f'Invalid template package path {namespace!r}')
        standard = manifest.get('standard_metadata')
        assumptions = manifest.get('template_assumptions')
        if not isinstance(standard, dict) or not isinstance(assumptions, dict):
            raise ValueError(f'{namespace}: direct template package metadata is required')
        standard_namespace = f"{standard.get('country')}/{standard.get('id')}"
        if not standard.get('country') or not standard.get('id') or standard_namespace in library['standards']:
            raise ValueError(f'{namespace}: invalid or duplicate standard metadata')
        package = folder / namespace
        files = sorted(package.glob('*.json'))
        if not files:
            raise ValueError(f'{namespace}: no direct building templates found')
        templates = {}
        for filename in files:
            template_id, record = _read_direct_template(filename, package, standard, assumptions)
            if template_id in templates:
                raise ValueError(f'Duplicate template id {template_id!r}')
            templates[template_id] = record
        library['standards'][standard_namespace] = {'templates': templates}
    return library


def _read_direct_template(filename, package, standard, assumptions):
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
    resolved = _load_schedule_sources(original, package, template_id)
    if set(resolved['Space']) != {'air_exchange', 'internal_gains', 'system'}:
        raise ValueError(f'{filename.name}: invalid Space domains')
    for domain in ('air_exchange', 'internal_gains', 'system'):
        _validate_parameter_sources(resolved['Space'][domain], f'{template_id}/Space/{domain}', resolved=True)
    expected_groups = {
        'exterior_wall', 'interior_wall', 'floor', 'ground', 'roof', 'window', 'skylight'}
    if set(resolved['Envelope']) != expected_groups:
        raise ValueError(f'{filename.name}: invalid Envelope construction groups')
    allowed_parameters = {
        'u', 'shgc', 'visible_reflectance', 'visible_transmittance',
        'air_leakage_per_area', 'opening_ratio'}
    for construction, parameters in resolved['Envelope'].items():
        if not isinstance(parameters, dict) or set(parameters) - allowed_parameters:
            raise ValueError(f'{template_id}/Envelope/{construction}: invalid construction properties')
        _validate_parameter_sources(
            parameters, f'{template_id}/Envelope/{construction}', resolved=True)
    profile = {
        'schema': 'moosas.building-template',
        'version': 4,
        'id': template_id,
        'standard': deepcopy(standard),
        'BuildingTemplate': {'Space': resolved['Space'], 'Envelope': resolved['Envelope']},
        'metadata': {
            'standard_source': deepcopy(standard),
            'template_metadata': deepcopy(metadata),
            'energy_assumptions': deepcopy(assumptions),
        },
    }
    return template_id, profile


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
                         or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                                for v in value['values'])):
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


def _load_schedule_sources(value, package_path, label):
    if isinstance(value, dict) and value.get('mode') in {'constant', 'schedule'}:
        _validate_value_source(value, label)
        if value['mode'] == 'constant':
            return deepcopy(value)
        schedule_file = Path(value['file'])
        resolved = (package_path / schedule_file).resolve()
        if package_path.resolve() not in resolved.parents:
            raise ValueError(f'{label}: schedule path escapes its template package')
        document = json.loads(resolved.read_text(encoding='utf-8'))
        if (not isinstance(document, dict) or document.get('schema') != 'moosas.schedule'
                or document.get('version') != 1):
            raise ValueError(f'{label}: unsupported .sch schema/version')
        schedules = document.get('schedules', {})
        if not isinstance(schedules, dict):
            raise ValueError(f'{label}: .sch schedules must be an object')
        if value['name'] not in schedules:
            raise ValueError(f'{label}: schedule {value["name"]!r} is missing from {value["file"]}')
        item = schedules[value['name']]
        if not isinstance(item, dict):
            raise ValueError(f'{label}: schedule entry must be an object')
        values = item.get('values')
        if item.get('unit') != value['unit']:
            raise ValueError(f'{label}: schedule unit does not match template parameter unit')
        if (not isinstance(values, list) or len(values) != 8760 or any(
                isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                for v in values)):
            raise ValueError(f'{label}: schedule must contain exactly 8760 finite values')
        return {**deepcopy(value), 'values': values}
    if isinstance(value, dict):
        return {key: _load_schedule_sources(item, package_path, f'{label}/{key}')
                for key, item in value.items()}
    if isinstance(value, list):
        return [_load_schedule_sources(item, package_path, f'{label}/{index}')
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
        result.update(deepcopy(package['templates']))
    return result


def validate_template_snapshot(settings):
    """Reject obsolete snapshots rather than silently reinterpreting their rules."""
    if '_thermal_template' in settings:
        raise ValueError('Legacy thermal template snapshots are unsupported; reapply the building template explicitly')
    profile = settings.get('_building_template')
    if profile is None:
        return
    if profile.get('schema') != 'moosas.building-template' or profile.get('version') != 4:
        raise ValueError('Unsupported building template snapshot; explicitly reapply a COUNTRY/STANDARD/template/type template')
    if 'schedules' in profile.get('BuildingTemplate', {}).get('Space', {}):
        raise ValueError('BuildingTemplate v4 stores schedules on individual parameters; reapply the template explicitly')
    standard = profile['standard']
    if not profile['id'].startswith(f"{standard['country']}/{standard['id']}/"):
        raise ValueError('Building template snapshot id does not match its standard namespace')
    if 'zone_template' in settings and settings['zone_template'] != profile['id']:
        raise ValueError('zone_template does not match its building template snapshot; reapply the template explicitly')
    _validate_template_snapshot(profile)


def _validate_template_snapshot(profile):
    template = profile.get('BuildingTemplate', {})
    if set(template) != {'Space', 'Envelope'}:
        raise ValueError('Direct BuildingTemplate snapshot requires Space and Envelope domains')
    space = template['Space']
    if set(space) != {'air_exchange', 'internal_gains', 'system'}:
        raise ValueError('Direct BuildingTemplate Space snapshot has invalid domains')
    for domain in ('air_exchange', 'internal_gains', 'system'):
        _validate_parameter_sources(space[domain], f'Space/{domain}', resolved=True)
    envelope = template['Envelope']
    expected_groups = {
        'exterior_wall', 'interior_wall', 'floor', 'ground', 'roof', 'window', 'skylight'}
    if set(envelope) != expected_groups:
        raise ValueError('Direct BuildingTemplate Envelope snapshot has invalid construction groups')
    allowed_parameters = {
        'u', 'shgc', 'visible_reflectance', 'visible_transmittance',
        'air_leakage_per_area', 'opening_ratio'}
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


def apply_building_template_to_space(space, record, name, *, preserve_explicit=True):
    """Apply a JSON record; keep the expanded snapshot portable in model IO."""
    if record.get('schema') != 'moosas.building-template' or record.get('version') != 4:
        raise ValueError('This API requires a version 4 BuildingTemplate record')
    if record.get('id') != name:
        raise ValueError('BuildingTemplate id does not match the selected template')
    if not preserve_explicit and hasattr(space, 'explicit_settings'):
        space.explicit_settings = [k for k in space.explicit_settings if not k.startswith('zone_')]
    space.settings['_building_template'] = deepcopy(record)
    space.settings['zone_template'] = name


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
    if record.get('schema') != 'moosas.building-template':
        raise ValueError('This API requires a structured JSON building template')
    selected = set(map(str, zone_ids)) if zone_ids is not None else {str(s.id) for s in model.spaceList}
    if selected - {str(s.id) for s in model.spaceList}:
        raise ValueError('Unknown zone_ids in template application')
    model.buildingTemplate.update(records)
    for space in model.spaceList:
        if str(space.id) in selected:
            apply_building_template_to_space(
                space, record, template_id, preserve_explicit=preserve_explicit)
    return model


def template_program(settings, key, base):
    """Return an individual parameter's explicit 8760-hour schedule."""
    value = template_parameter(settings, key)
    if isinstance(value, list) and len(value) == 8760:
        import numpy as np
        return np.asarray(value, dtype=float)
    return None


def template_parameter(settings, key):
    """Return a template parameter's constant or resolved hourly values."""
    validate_template_snapshot(settings)
    profile = settings.get('_building_template')
    if not profile:
        return None
    paths = {
        'zone_pfav': ('air_exchange', 'fresh_air'),
        'zone_infiltration': ('air_exchange', 'infiltration'),
        'zone_ppsm': ('internal_gains', 'occupancy_density'),
        'zone_popheat': ('internal_gains', 'occupant_heat'),
        'zone_lighting': ('internal_gains', 'lighting_power_density'),
        'zone_equipment': ('internal_gains', 'equipment_power_density'),
        'zone_h_temp': ('system', 'heating_setpoint'),
        'zone_c_temp': ('system', 'cooling_setpoint'),
        'zone_collingEER': ('system', 'cooling_cop'),
        'zone_HeatingEER': ('system', 'heating_cop'),
        'hvac_enabled': ('system', 'hvac_availability'),
    }
    if key not in paths:
        return None
    domain, name = paths[key]
    source = profile['BuildingTemplate']['Space'][domain][name]
    return source['value'] if source['mode'] == 'constant' else source['values']


def resolve_surface_template(settings, role, boundary):
    """Map the selected construction group's direct values to an Energy surface."""
    validate_template_snapshot(settings)
    profile = settings.get('_building_template')
    if not profile:
        return {}, ()
    assumptions = profile['metadata']['energy_assumptions']
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
    parameters = profile['BuildingTemplate']['Envelope'][construction]
    glass = role in {'window', 'skylight'}
    out = dict(assumptions['glass' if glass else 'slab' if role in {'floor', 'roof', 'ceiling'} else 'wall'])
    out['_template_id'] = profile['id']
    notes = []
    if boundary == 'adiabatic':
        out['resistance_m2k_w'] = assumptions['internal_resistance_m2k_w']
    elif boundary == 'ground':
        out['resistance_m2k_w'] = (
            assumptions['ground_base_resistance_m2k_w']
            + assumptions['unspecified_ground_insulation_r'])
        notes.append('Ground-contact construction has no direct U-value; an explicitly declared engineering resistance is used.')
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
