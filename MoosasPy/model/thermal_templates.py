"""Versioned JSON thermal libraries and explicit, auditable Energy projections.

The standard specifies limits, not material recipes. Equivalent constructions
and operation simplifications are recorded separately in the library.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path


DEFAULT_LIBRARY = Path(__file__).resolve().parents[1] / 'db' / 'building_template.json'


def load_thermal_library(filename=None):
    filename = Path(filename or DEFAULT_LIBRARY)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'Duplicate thermal template key: {key}')
            result[key] = value
        return result
    data = json.loads(filename.read_text(encoding='utf-8'), object_pairs_hook=unique,
                      parse_constant=lambda x: (_ for _ in ()).throw(ValueError(f'Invalid JSON number {x}')))
    if data.get('schema') != 'moosas.thermal-templates' or data.get('version') != 1:
        raise ValueError('Unsupported thermal template schema/version')
    required = {'standard', 'envelopes', 'operations', 'assumptions', 'templates', 'aliases', 'legacy_templates'}
    if not required <= data.keys():
        raise ValueError(f'Missing thermal library sections: {required - data.keys()}')
    def check(value, key='root'):
        if isinstance(value, dict):
            for k, v in value.items(): check(v, f'{key}/{k}')
        elif isinstance(value, list):
            for v in value: check(v, key)
        elif isinstance(value, (int, float)) and (not math.isfinite(value) or value < 0):
            raise ValueError(f'{key}: expected finite nonnegative value')
    check(data)
    for name, spec in data['templates'].items():
        if spec['envelope'] not in data['envelopes'] or spec['operation'] not in data['operations']:
            raise ValueError(f'{name}: unknown envelope/operation reference')
    for alias, name in data['aliases'].items():
        if name not in data['templates']:
            raise ValueError(f'{alias}: unknown alias target {name}')
    for name, rule in data['envelopes'].items():
        for field in ('roof_u', 'wall_u', 'window_u'):
            if field not in rule:
                raise ValueError(f'{name}: missing {field}')
        for field in ('roof_u', 'wall_u', 'window_u', 'skylight_u', 'exposed_floor_u'):
            value = rule.get(field)
            if value is not None and any(v <= 0 for v in _numbers(value)):
                raise ValueError(f'{name}/{field}: U must be positive')
        if any(v > 1 for v in _numbers(rule.get('window_shgc', []))):
            raise ValueError(f'{name}: SHGC must be in 0..1')
        bins = rule.get('wwr_bins', [])
        if bins != sorted(set(bins)) or any(not 0 < b <= 1 for b in bins):
            raise ValueError(f'{name}: invalid WWR bins')
        if bins and len(rule['window_u']) != len(bins):
            raise ValueError(f'{name}: window U rows do not match WWR bins')
        shgc = rule.get('window_shgc')
        if shgc and len(shgc) != len(bins):
            raise ValueError(f'{name}: SHGC rows do not match WWR bins')
        for field in ('roof_u','wall_u','exposed_floor_u'):
            if isinstance(rule.get(field),list) and len(rule[field]) != 2:
                raise ValueError(f'{name}/{field}: conditional column requires two values')
    for role in ('glass','wall','slab'):
        material = data['assumptions'][role]
        rho, tau = material.get('solar_reflectance',0),material.get('solar_transmittance',0)
        if rho+tau>1 or any(not 0<=material[k]<=1 for k in ('outside_emissivity','inside_emissivity')):
            raise ValueError(f'{role}: invalid optical coefficients')
    for name, op in data['operations'].items():
        if op['zone_ppsm'] <= 0 or op['zone_h_temp'] > op['zone_c_temp']:
            raise ValueError(f'{name}: invalid density/setpoints')
        for field in ('occupancy', 'lighting', 'equipment', 'hvac'):
            for day in ('weekday', 'weekend'):
                values = op['profiles'][field][day]
                if len(values) != 24 or any(not 0 <= x <= 1 for x in values):
                    raise ValueError(f'{name}/{field}/{day}: requires 24 factors in 0..1')
    return data


def _numbers(value):
    if isinstance(value, list):
        return [n for v in value for n in _numbers(v)]
    return [] if value is None else [float(value)]


def building_templates(filename=None):
    library = load_thermal_library(filename)
    result = deepcopy(library['legacy_templates'])
    for name, spec in library['templates'].items():
        op = library['operations'][spec['operation']]
        op = deepcopy(op)
        if spec['operation'] == 'RESIDENTIAL':
            cold = 'severe_cold' in spec['envelope'] or spec['envelope'].startswith('residential_cold')
            op['zone_infiltration'] = op['zone_nightACH'] = .5 if cold else 1.
            op['zone_collingEER'] = 3.6
        payload = dict(id=name, standard=deepcopy(library['standard']),
                       envelope=deepcopy(library['envelopes'][spec['envelope']]),
                       operation=deepcopy(op), assumptions=deepcopy(library['assumptions']),
                       context=deepcopy(spec.get('context', {})))
        result[name] = {k: v for k, v in op.items() if k.startswith('zone_')}
        result[name].update(type=spec['operation'], standard=library['standard']['id'],
                            _thermal_template=payload)
    for alias, target in library['aliases'].items():
        result[alias] = deepcopy(result[target])
        result[alias]['_thermal_template']['alias'] = alias
    return result


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

    These are simplified occupied operation scenarios, not a complete Appendix C
    compliance calculation (holidays, ramp-up and room weighting are excluded).
    """
    profile = settings.get('_thermal_template')
    if not profile:
        return None
    mapping = {'zone_ppsm': 'occupancy', 'zone_lighting': 'lighting',
               'zone_equipment': 'equipment', 'hvac_enabled': 'hvac'}
    if key not in mapping or isinstance(base, str) or not isinstance(base, (float, int)):
        return None
    import numpy as np
    day = profile['operation']['profiles'][mapping[key]]
    week = np.array(day['weekday'] * 5 + day['weekend'] * 2, dtype=float)
    return np.resize(week, 8760) * float(base)


def resolve_surface_template(settings, role, boundary, normal, *, wwr=0., context=None):
    """Project an envelope limit to an equivalent construction in SI units.

    Unknown storeys, shape or inertia use the stricter table column, never an
    inferred material property. Ground R is insulation-only, added to base R.
    """
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
        selector = rule.get('column', '')
        parameter = {'storeys': 'storeys', 'shape_factor': 'shape_factor', 'inertia': 'thermal_inertia_D'}.get(selector)
        if parameter in ctx:
            threshold = {'storeys': 3, 'shape_factor': .3, 'inertia': 2.5}[selector]
            return value[0 if ctx[parameter] <= threshold else 1]
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
            shgc = rule.get('skylight_shgc') if role == 'skylight' else (rule.get('window_shgc') or [None] * max(1,len(bins)))[index]
            if isinstance(shgc, list):
                # Geographic north is +Y. Rotation is supplied by the adapter.
                angle = math.degrees(math.atan2(normal[0], normal[1])) % 360
                north = min(angle, 360-angle) < rule.get('north_sector_degrees', 30)
                south = 150 <= angle <= 210
                if rule.get('shgc_directions') == 'W_ES_N':
                    shgc = shgc[2 if north else 0 if 210 < angle < 330 else 1]
                elif rule.get('shgc_directions') == 'EW_S_N':
                    shgc = shgc[2 if north else 1 if south else 0]
                else:
                    shgc = shgc[1 if north else 0]
            if shgc is not None:
                limits['summer_shgc_max'] = shgc
                # Conservative summer equivalent: tau+alpha <= SHGC limit.
                # SHGC itself is NEVER passed as shortwave transmittance.
                alpha = min(assumptions['glass_absorptance'], shgc)
                out['solar_transmittance'] = max(0., shgc-alpha)
                out['solar_reflectance'] = 1-shgc
            winter = rule.get('skylight_winter_shgc_min') if role=='skylight' else rule.get('winter_shgc_min')
            if isinstance(winter,list): winter=winter[index]
            if role=='window' and rule.get('shgc_directions')=='EW_S_N' and isinstance(shgc,(int,float)) and (north or south):
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
    selector_parameter = {'storeys':'storeys','shape_factor':'shape_factor','inertia':'thermal_inertia_D'}.get(rule.get('column'))
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
