"""Versioned JSON thermal libraries and explicit, auditable Energy projections.

The standard specifies limits, not material recipes. Equivalent constructions
and operation simplifications are recorded separately in the library.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path


DEFAULT_LIBRARY = Path(__file__).resolve().parents[1] / 'db' / 'template'


def _read_library_file(filename, schema='moosas.thermal-templates'):
    data = json.loads(filename.read_text(encoding='utf-8'))
    if data.get('schema') != schema or data.get('version') != 2:
        raise ValueError(f'Unsupported thermal template schema/version in {filename.name}')
    return data


def load_thermal_library(library_path=None):
    """Load explicitly listed country/standard packages; no standard is selected."""
    folder = Path(library_path or DEFAULT_LIBRARY)
    manifest = _read_library_file(folder / 'library.json', 'moosas.thermal-library')
    library = dict(schema=manifest['schema'], version=manifest['version'], standards={})
    for namespace in manifest['standards']:
        parts = namespace.split('/')
        if len(parts) != 2 or any(not p or p in {'.', '..'} or '\\' in p or ':' in p for p in parts):
            raise ValueError(f'Invalid standard namespace {namespace!r}; expected COUNTRY/STANDARD-VERSION')
        if namespace in library['standards']:
            raise ValueError(f'Duplicate standard namespace {namespace!r}')
        package = folder.joinpath(*parts)
        common = _read_library_file(package / 'common.json')
        if parts != [common['standard']['country'], common['standard']['id']]:
            raise ValueError(f'{namespace}: standard metadata does not match package namespace')
        if common.get('unit_system') != 'SI':
            raise ValueError(f'{namespace}: template values must use SI units')
        _validate_projection(common)
        common['operations'] = {}
        common['templates'] = {}
        for filename in common['building_types']:
            if not filename or Path(filename).name != filename:
                raise ValueError(f'{namespace}: building type must be a local JSON filename')
            spec = _read_library_file(package / filename)
            op_id = spec['operation_id']
            if not op_id or '/' in op_id or op_id in common['operations']:
                raise ValueError(f'{namespace}: invalid or duplicate operation id {op_id!r}')
            _validate_operation(spec, namespace)
            common['operations'][op_id] = spec['operation']
            for envelope in spec['envelopes']:
                if envelope not in common['envelopes']:
                    raise ValueError(f'{namespace}/{filename}: unknown envelope {envelope!r}')
                template_id = f'{namespace}/{envelope}/{op_id}'
                if template_id in common['templates']:
                    raise ValueError(f'Duplicate template id {template_id!r}')
                common['templates'][template_id] = dict(
                    envelope=envelope, operation=op_id, wwr_scope=spec['wwr_scope'],
                    operation_overrides=spec.get('operation_overrides', {}).get(envelope, {}))
        library['standards'][namespace] = common
    return library


def _validate_projection(package):
    if package.get('projection') != 'prescriptive-envelope-v1':
        raise ValueError(f"Unsupported template projection {package.get('projection')!r}")
    supported = {
        'table', 'applicability', 'wall_u', 'roof_u', 'exposed_floor_u', 'window_u',
        'window_shgc', 'skylight_u', 'skylight_shgc', 'winter_shgc_min',
        'skylight_winter_shgc_min', 'ground_insulation_r', 'underground_wall_insulation_r',
        'wwr_bins', 'column_selector', 'shgc_sectors', 'shgc_default_index',
        'maximum_shape_factor', 'maximum_skylight_ratio', 'internal_floor_u',
        'internal_partition_u', 'unheated_basement_ceiling_u', 'unheated_partition_u',
        'unheated_stair_partition_u',
    }
    for name, envelope in package['envelopes'].items():
        if not name or '/' in name:
            raise ValueError(f'Invalid envelope id {name!r}')
        unknown = set(envelope) - supported
        if unknown:
            raise ValueError(f'{name}: unsupported envelope rules {sorted(unknown)}')
        selector = envelope.get('column_selector')
        if selector is not None:
            if set(selector) != {'parameter', 'threshold'} or not isinstance(selector['parameter'], str):
                raise ValueError(f'{name}: column_selector requires parameter and threshold')
            if not isinstance(selector['threshold'], (int, float)) or not math.isfinite(selector['threshold']):
                raise ValueError(f'{name}: column_selector threshold must be finite')
        bins = envelope.get('wwr_bins', [])
        if bins != sorted(set(bins)) or any(not 0 <= v <= 1 for v in bins):
            raise ValueError(f'{name}: wwr_bins must be strictly increasing within 0..1')
        if 'shgc_sectors' in envelope:
            default_index = envelope.get('shgc_default_index')
            if type(default_index) is not int or default_index < 0:
                raise ValueError(f'{name}: shgc_sectors require a nonnegative default index')
            for sector in envelope['shgc_sectors']:
                required = {'start', 'end', 'include_start', 'include_end', 'index'}
                if not required <= set(sector) or set(sector) - required - {'winter_shgc'}:
                    raise ValueError(f'{name}: unsupported SHGC sector rule')
                if not all(isinstance(sector[k], (int, float)) and 0 <= sector[k] < 360
                           for k in ('start', 'end')):
                    raise ValueError(f'{name}: SHGC sector angles must be within 0..360')
                if (type(sector['index']) is not int or sector['index'] < 0 or
                        any(type(sector[k]) is not bool for k in ('include_start', 'include_end')) or
                        ('winter_shgc' in sector and type(sector['winter_shgc']) is not bool)):
                    raise ValueError(f'{name}: invalid SHGC sector index or flags')


def _validate_operation(spec, namespace):
    label = f"{namespace}/{spec['operation_id']}"
    if spec['wwr_scope'] not in {'zone', 'building'}:
        raise ValueError(f'{label}: unsupported wwr_scope {spec["wwr_scope"]!r}')
    operation = spec['operation']
    week = operation['week']
    if len(week) != 7:
        raise ValueError(f'{label}: week requires seven day references, Monday first')
    for role in ('occupancy', 'lighting', 'equipment', 'hvac'):
        days = operation['profiles'][role]
        if set(week) - set(days):
            raise ValueError(f'{label}/{role}: week references an unknown day profile')
        for day, values in days.items():
            if len(values) != 24 or any(
                    not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1
                    for v in values):
                raise ValueError(f'{label}/{role}/{day}: expected 24 finite fractions within 0..1')
    for envelope, overrides in spec.get('operation_overrides', {}).items():
        if envelope not in spec['envelopes'] or any(
                not key.startswith('zone_') or key not in operation or
                not isinstance(value, (int, float)) or not math.isfinite(value)
                for key, value in overrides.items()):
            raise ValueError(f'{label}/{envelope}: invalid operation_overrides')


def configure_model_resources(model, template_path=None):
    """List available templates on a model without applying any of them."""
    if template_path is not None or not model.buildingTemplate:
        model.buildingTemplate = building_templates(template_path)
    return model


def _strictest(value):
    """Stricter table limit for unknown columns: the lowest numeric leaf."""
    if isinstance(value, list):
        numbers = [n for n in (_strictest(v) for v in value) if n is not None]
        return min(numbers) if numbers else None
    return value


def building_templates(library_path=None):
    library = load_thermal_library(library_path)
    result = {}
    for namespace, package in library['standards'].items():
        for name, spec in package['templates'].items():
            op = deepcopy(package['operations'][spec['operation']])
            op.update(spec['operation_overrides'])
            envelope = package['envelopes'][spec['envelope']]
            payload = dict(schema='moosas.thermal-template', version=2, id=name,
                           standard=deepcopy(package['standard']), projection=package['projection'],
                           envelope=deepcopy(envelope), wwr_scope=spec['wwr_scope'],
                           operation=op, assumptions=deepcopy(package['assumptions']), context={})
            result[name] = {k: v for k, v in op.items() if k.startswith('zone_')}
            shgc = _strictest(envelope.get('window_shgc'))
            result[name].update(
                zone_wallU=_strictest(envelope['wall_u']),
                zone_winU=_strictest(envelope['window_u']),
                zone_win_SHGC=shgc if shgc is not None else 1. - package['assumptions']['glass']['solar_reflectance'],
            )
            result[name].update(type=spec['operation'], standard=namespace, _thermal_template=payload)
    return result


def validate_template_snapshot(settings):
    """Reject obsolete snapshots rather than silently reinterpreting their rules."""
    profile = settings.get('_thermal_template')
    if profile is None:
        return
    if profile.get('schema') != 'moosas.thermal-template' or profile.get('version') != 2:
        raise ValueError('Unsupported thermal template snapshot; explicitly reapply a COUNTRY/STANDARD/template/type template')
    standard = profile['standard']
    if not profile['id'].startswith(f"{standard['country']}/{standard['id']}/"):
        raise ValueError('Thermal template snapshot id does not match its standard namespace')
    if 'zone_template' in settings and settings['zone_template'] != profile['id']:
        raise ValueError('zone_template does not match its thermal template snapshot; reapply the template explicitly')
    if profile.get('projection') != 'prescriptive-envelope-v1':
        raise ValueError(f"Unsupported template projection {profile.get('projection')!r}")
    if profile.get('wwr_scope') not in {'zone', 'building'}:
        raise ValueError(f"Unsupported template wwr_scope {profile.get('wwr_scope')!r}")


def apply_thermal_template(space, record, name, *, preserve_explicit=True, context=None):
    """Apply a JSON record; keep the expanded snapshot portable in model IO."""
    explicit = set(getattr(space, 'explicit_settings', ())) if preserve_explicit else set()
    if not preserve_explicit and hasattr(space, 'explicit_settings'):
        space.explicit_settings = [k for k in space.explicit_settings if k not in record or not k.startswith('zone_')]
    for key, value in record.items():
        if key not in explicit:
            space.settings[key] = deepcopy(value)
    space.settings['zone_template'] = name
    if context:
        space.settings['_thermal_template']['context'].update(context)


def apply_building_template(model, template_id, *, template_path=None, zone_ids=None,
                            preserve_explicit=True, context=None):
    """Select JSON templates explicitly. Geometry and source files are unchanged.

    ``preserve_explicit=False`` rebases zone operating values only. Element
    constructions remain authoritative; use surface overrides to replace them.
    """
    records = building_templates(template_path)
    if template_id not in records:
        raise ValueError(f'Unknown building template {template_id!r}')
    record = records[template_id]
    if '_thermal_template' not in record:
        raise ValueError('This API requires a structured JSON thermal template')
    selected = set(map(str, zone_ids)) if zone_ids is not None else {str(s.id) for s in model.spaceList}
    if selected - {str(s.id) for s in model.spaceList}:
        raise ValueError('Unknown zone_ids in template application')
    model.buildingTemplate.update(records)
    for space in model.spaceList:
        if str(space.id) in selected:
            apply_thermal_template(space, record, template_id, preserve_explicit=preserve_explicit, context=context)
    return model


def template_program(settings, key, base):
    """Return an absolute weekly vector; Monday starts at hour zero.

    Day references belong to the selected standard, not a global weekend rule.
    """
    validate_template_snapshot(settings)
    profile = settings.get('_thermal_template')
    if not profile:
        return None
    mapping = {'zone_ppsm': 'occupancy', 'zone_lighting': 'lighting',
               'zone_equipment': 'equipment', 'hvac_enabled': 'hvac'}
    if key not in mapping or isinstance(base, str) or not isinstance(base, (float, int)):
        return None
    import numpy as np
    day = profile['operation']['profiles'][mapping[key]]
    week = np.array([value for name in profile['operation']['week'] for value in day[name]], dtype=float)
    return np.resize(week, 8760) * float(base)


def resolve_surface_template(settings, role, boundary, normal, *, wwr=0., context=None):
    """Project an envelope limit to an equivalent construction in SI units.

    Unknown storeys, shape or inertia use the stricter table column, never an
    inferred material property. Ground R is insulation-only, added to base R.
    """
    validate_template_snapshot(settings)
    profile = settings.get('_thermal_template')
    if not profile:
        return {}, ()
    rule, assumptions = profile['envelope'], profile['assumptions']
    ctx = {**profile.get('context', {}), **(context or {})}
    notes = []
    if rule.get('applicability'):
        notes.append(rule['applicability'])
    for key in ('storeys','shape_factor','thermal_inertia_D','window_wall_ratio'):
        if key in ctx and (not isinstance(ctx[key],(int,float)) or not math.isfinite(ctx[key]) or ctx[key]<0):
            raise ValueError(f'Template context {key}: expected finite nonnegative value')
    if 'storeys' in ctx and (ctx['storeys'] < 1 or int(ctx['storeys']) != ctx['storeys']):
        raise ValueError('Template storeys must be a positive integer')
    if ctx.get('window_wall_ratio',wwr)>1+1e-9:
        raise ValueError('Template window_wall_ratio must be in 0..1')
    wwr = min(1.,max(0.,wwr))
    if rule.get('maximum_shape_factor') is not None and ctx.get('shape_factor',0)>rule['maximum_shape_factor']:
        notes.append('Shape factor exceeds prescriptive table applicability; this is a simulation scenario requiring a separate compliance assessment.')
    def column(value):
        if not isinstance(value, list): return value
        selector = rule['column_selector']
        parameter = selector['parameter']
        if parameter in ctx:
            return value[0 if ctx[parameter] <= selector['threshold'] else 1]
        return min(v for v in value if v is not None)
    glass = role in {'window', 'skylight'}
    out = dict(assumptions['glass' if glass else 'slab' if role in {'floor','roof','ceiling'} else 'wall'])
    out['_template_id'] = profile['id']
    limits = {}
    if boundary == 'adiabatic':
        # Independent zones retain room-side mass; no exterior U requirement.
        out['resistance_m2k_w'] = assumptions['internal_resistance_m2k_w']
    elif boundary == 'ground':
        minimum = rule.get('ground_insulation_r' if role == 'floor' else 'underground_wall_insulation_r')
        out['resistance_m2k_w'] = assumptions['ground_base_resistance_m2k_w'] + (minimum or assumptions['unspecified_ground_insulation_r'])
        if minimum is not None: limits['insulation_r_min'] = minimum
        notes.append('Ground insulation minimum is insulation-only; base construction R is an engineering assumption. Peripheral-floor R is applied to the full ground floor as a scenario assumption.')
    else:
        if glass:
            bins = rule.get('wwr_bins', [])
            index = next((i for i, b in enumerate(bins) if wwr <= b + 1e-9), max(0,len(bins)-1))
            if role=='window' and bins and wwr > bins[-1]:
                notes.append('WWR exceeds prescriptive table applicability; last row is used for simulation only.')
            u = rule.get('skylight_u') if role == 'skylight' else column(rule['window_u'][index] if bins else rule['window_u'])
            window_shgc = rule.get('window_shgc')
            shgc = rule.get('skylight_shgc') if role == 'skylight' else (
                window_shgc[index] if isinstance(window_shgc, list) else window_shgc)
            winter_applicable = True
            if isinstance(shgc, list):
                # Geographic north is +Y. Rotation is supplied by the adapter.
                angle = math.degrees(math.atan2(normal[0], normal[1])) % 360
                direction = rule['shgc_default_index']
                for sector in rule['shgc_sectors']:
                    after = angle >= sector['start'] if sector['include_start'] else angle > sector['start']
                    before = angle <= sector['end'] if sector['include_end'] else angle < sector['end']
                    matches = (after or before) if sector['start'] > sector['end'] else (after and before)
                    if matches:
                        direction = sector['index']
                        winter_applicable = sector.get('winter_shgc', True)
                        break
                shgc = shgc[direction]
            if shgc is not None:
                limits['summer_shgc_max'] = shgc
                # Conservative summer equivalent: tau+alpha <= SHGC limit.
                # SHGC itself is NEVER passed as shortwave transmittance.
                alpha = min(assumptions['glass_absorptance'], shgc)
                out['solar_transmittance'] = max(0., shgc-alpha)
                out['solar_reflectance'] = 1-shgc
            winter = rule.get('skylight_winter_shgc_min') if role=='skylight' else rule.get('winter_shgc_min')
            if isinstance(winter,list): winter=winter[index]
            if role == 'window' and isinstance(shgc, (int, float)) and not winter_applicable:
                winter=None
            if winter is not None:
                limits['winter_shgc_min'] = winter
                notes.append('Summer-equivalent optics do not establish winter SHGC compliance; supply product optics and seasonal exterior shading for that assessment.')
        else:
            field = 'roof_u' if role in {'roof','ceiling'} else 'exposed_floor_u' if role == 'floor' else 'wall_u'
            u = column(rule.get(field))
        if u is not None:
            out['u_value'] = u
            out['u_convention'] = 'air_to_air'
            limits['u_max_w_m2k'] = u
        else:
            out['resistance_m2k_w'] = assumptions['unspecified_resistance_m2k_w']
            notes.append('This table does not prescribe this component; equivalent R uses a declared engineering assumption.')
    out['_template_limits'] = limits
    out['_template_source'] = rule['table']
    selector_parameter = rule.get('column_selector', {}).get('parameter')
    out['_template_context'] = {**{k:str(v) for k,v in ctx.items()}, 'window_wall_ratio':str(wwr),
                                'column_selection': 'provided_context' if selector_parameter in ctx else 'stricter_limit' if selector_parameter else 'none'}
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
