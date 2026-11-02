"""Resolve Moosas geometry and programs into explicit surface-side requests."""
from __future__ import annotations

from dataclasses import replace
from functools import lru_cache
import math

import numpy as np
import shapely

from ...model.resources import get_schedule_name, load_schedule
from ...model.thermal_templates import building_templates, resolve_surface_template, merge_surface_template, template_program
from ...transform.geometry.geos import Projection, Vector
from ...utils.tools import path
from .requests import EnergyDefaults, EnergyRequest, LayerRequest, SurfaceRequest, ZoneRequest, readonly


def number(value, label, *, minimum=0., strictly_positive=False):
    try:
        value = float(value)
    except (ValueError, TypeError) as error:
        raise ValueError(f'{label}: expected a numeric SI value, got {value!r}') from error
    if not math.isfinite(value) or value < minimum or (strictly_positive and value <= minimum):
        raise ValueError(f'{label}: invalid value {value}')
    return value


def expand_schedule(model, value, label, cache=None):
    """Existing schedules contain absolute values, never implicit multipliers."""
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
            elif mode in {'daily','hourly'}:
                expected = 24 if mode == 'daily' else 8760
                if len(values) != expected:
                    raise ValueError(f'{label}: {mode} requires {expected} values')
                result = np.tile(np.asarray(values, dtype=float), 365) if mode == 'daily' else np.asarray(values, dtype=float)
            else:
                raise ValueError(f'{label}: unsupported schedule mode {mode}')
            if item.get('value_mode', 'absolute') != 'absolute':
                raise ValueError(f'{label}: schedule multipliers require explicit conversion to absolute values')
            result = readonly(result)
            cache[name] = result
            return result
    if np.isscalar(value):
        return readonly(np.full(8760, float(value)))
    result = readonly(value)
    if result.shape != (8760,):
        raise ValueError(f'{label}: requires 8760 hourly values')
    return result


def split_layers(layers, side):
    """Return the room-facing half, ordered from the adiabatic cut to the room.

Full layer input is ordered along the element normal, from back to front.
Side 0 faces the front, side 1 the back. Physical thickness selects the cut;
when thickness is unavailable resistance is the declared equivalent metric.
"""
    ordered = list(layers if side == 0 else reversed(layers))
    widths = [l.thickness_m for l in ordered]
    if not all(w > 0 for w in widths):
        widths = [l.resistance_m2k_w for l in ordered]
    cut = sum(widths) / 2
    result = []
    cursor = 0.
    for layer, width in zip(ordered, widths):
        fraction = max(0., cursor + width - max(cursor, cut)) / width
        if fraction > 1e-12:
            result.append(LayerRequest(layer.resistance_m2k_w * fraction,
                                       layer.capacity_j_m2k * fraction,
                                       layer.thickness_m * fraction))
        cursor += width
    return tuple(result)


def _ground_parts(geometry, projection, ground):
    """Clip the UV polygon by the world-space ground plane."""
    corners = list(shapely.box(*geometry.bounds).exterior.coords)[:-1]
    def elevation(point):
        return float(shapely.get_z(projection.toWorld(shapely.Point(point)))) - ground
    z = [elevation(p) for p in corners]
    if min(z) >= -1e-7:
        return (('outdoors', geometry),)
    if max(z) <= 1e-7:
        return (('ground', geometry),)
    clipped = []
    for start, end in zip(corners, corners[1:] + corners[:1]):
        a, b = elevation(start), elevation(end)
        if a <= 0:
            clipped.append(start)
        if (a <= 0) != (b <= 0):
            t = a / (a - b)
            clipped.append(tuple(start[i] + t * (end[i] - start[i]) for i in range(2)))
    below = shapely.intersection(geometry, shapely.Polygon(clipped))
    return (('ground', below), ('outdoors', shapely.difference(geometry, below)))


@lru_cache(maxsize=512)
def _room_region(geometry):
    """Regularize area only; touching legacy holes leave nonphysical linework."""
    region = shapely.force_2d(geometry)
    if not region.is_valid:
        stack = [shapely.make_valid(region)]
        polygons = []
        while stack:
            part = stack.pop()
            kind = shapely.get_type_id(part)
            if kind == 3: polygons.append(part)
            elif kind in (6,7): stack.extend(shapely.get_parts(part))
        repaired = shapely.union_all(polygons)
        if abs(repaired.area-region.area)>max(1e-8,abs(region.area)*1e-8):
            raise ValueError('Room footprint validity repair changes area; source geometry requires review')
        region = repaired
    # Mixing a fixed precision room with floating point buffer arcs can create
    # zero-length overlay edges in GEOS. Removing the precision *metadata*
    # leaves every coordinate intact and uses one consistent floating overlay.
    return shapely.set_precision(region,0)


def _height_band(footprint, projection, lo, hi):
    """Clip a UV surface to world Z limits, retaining its original holes."""
    x0,y0,x1,y1=footprint.bounds
    vertices=[np.array(p,float) for p in [(x0,y0),(x1,y0),(x1,y1),(x0,y1)]]
    a,b=float(projection.axisX[2]),float(projection.axisY[2])
    origin=float(projection.origin.array[2])
    for height,sign in ((lo,1.),(hi,-1.)):
        clipped=[]
        for start,end in zip(vertices,vertices[1:]+vertices[:1]):
            u=sign*(a*start[0]+b*start[1]+origin-height)
            v=sign*(a*end[0]+b*end[1]+origin-height)
            if u>=0:clipped.append(start)
            if (u>=0)!=(v>=0):clipped.append(start+(end-start)*u/(u-v))
        vertices=clipped
        if len(vertices)<3:return shapely.GeometryCollection()
    return shapely.intersection(footprint,shapely.Polygon(vertices))


def _owner_extent(space, projection, normal, center, footprint, *, height_bounds=None):
    """Intersect a surface plane with a room's vertical footprint extrusion."""
    if footprint.is_empty or footprint.area<=1e-12:
        return shapely.GeometryCollection()
    region = _room_region(space.force_2d())
    footprint = shapely.set_precision(footprint,0)
    if abs(normal[2]) > .999999:
        world = shapely.force_3d(region,z=float(center[2]))
        uv = shapely.force_2d(projection.toUV(world))
        return shapely.intersection(footprint,shapely.buffer(uv,1e-6,join_style='mitre'))
    # topLevel performs full floor/ceiling area validation. Repeating that for
    # every shared face/trace is quadratic for old whole-floor interfaces.
    # The cache belongs to one immutable build_energy_input request only.
    key=id(space)
    if height_bounds is None:
        lo,hi=float(space.level),float(space.topLevel)
    else:
        if key not in height_bounds:
            height_bounds[key]=(float(space.level),float(space.topLevel))
        lo,hi=height_bounds[key]
    if abs(normal[2]) > 1e-6:
        # General planar intersection with the room's footprint extrusion.
        # XY is an invertible affine map of UV for any nonvertical plane.
        # This covers both real inclined slabs and tiny legacy wall tilt.
        from shapely.affinity import affine_transform
        basis=np.column_stack((projection.axisX[:2],projection.axisY[:2]))
        inverse=np.linalg.inv(basis)
        offset=-inverse@projection.origin.array[:2]
        uv=affine_transform(shapely.buffer(region,1e-6,join_style='mitre'),
            [inverse[0,0],inverse[0,1],inverse[1,0],inverse[1,1],offset[0],offset[1]])
        patch=shapely.intersection(footprint,uv)
        if patch.is_empty:return patch
        return _height_band(patch,projection,lo,hi)
    tangent = np.array([-normal[1],normal[0]])
    span = max(footprint.bounds[2]-footprint.bounds[0],footprint.bounds[3]-footprint.bounds[1])*2+10
    polygons = []
    # Offset the line, not the room. Buffering touching holes creates phantom
    # ownership along their shared edge; curved micron buffers also triggered
    # zero-length GEOS overlay edges in legacy files. All three traces are
    # projected back onto the original surface plane before taking their union.
    xy_normal=normal[:2]/np.linalg.norm(normal[:2])
    for distance in (0.,1e-6,-1e-6):
        shift=xy_normal*distance
        line = shapely.LineString((center[:2]+shift-span*tangent,center[:2]+shift+span*tangent))
        crossing = shapely.intersection(line,region)
        for segment in shapely.get_parts(crossing):
            if shapely.get_dimensions(segment)!=1:
                continue
            xy = shapely.get_coordinates(segment)
            if len(xy)<2 or np.linalg.norm(xy[0]-xy[-1])<=1e-12:
                continue
            a,b = xy[0]-shift,xy[-1]-shift
            quad = shapely.Polygon(((a[0],a[1],lo),(b[0],b[1],lo),(b[0],b[1],hi),(a[0],a[1],hi)))
            polygons.append(shapely.force_2d(projection.toUV(quad)))
    patch=shapely.intersection(footprint,shapely.union_all(polygons))
    return patch if patch.area>1e-12 else shapely.GeometryCollection()


def _adjacency_parts(space, owner_ids, spaces, footprint, projection, normal, center, *, height_bounds=None):
    """Partition partial shared boundaries without using a zone-level outer flag."""
    own = _owner_extent(space,projection,normal,center,footprint,height_bounds=height_bounds)
    # Old shared Wall references include users outside the current face patch.
    # Their zero-area intersection has NaN bounds; never build a cut line from it.
    if own.is_empty or own.area<=1e-7:
        return []
    if footprint.area-own.area<1e-5:
        own=footprint
    shared=[]
    covered=shapely.GeometryCollection()
    for owner in sorted(owner_ids-{str(space.id)}):
        if owner not in spaces:
            raise ValueError(f'{space.id}: unknown boundary owner {owner}')
        region=_owner_extent(spaces[owner],projection,normal,center,own,height_bounds=height_bounds)
        if region.area<=1e-7:
            continue
        overlap = shapely.intersection(covered,region)
        # _owner_extent buffers each footprint by 1 micrometre to retain
        # boundary-coincident cuts. Adjacent owners therefore share a thin
        # numerical strip, with area proportional to their perimeter.
        # Reject real overlap, but remove this bounded strip exactly once.
        buffer_area_tolerance = 1e-6*(region.length+covered.length)+1e-10
        if overlap.area>max(1e-5,buffer_area_tolerance):
            raise ValueError(f'{space.id}: overlapping boundary owners need disjoint source geometry')
        if overlap.area:
            region=shapely.difference(region,covered)
        if region.area<=1e-7:
            continue
        if covered.is_empty and own.area-region.area<1e-5:
            region=own
        shared.append(('adiabatic',region,{str(space.id),owner}))
        covered=shapely.union(covered,region)
    remainder=shapely.difference(own,covered)
    if remainder.area>1e-5:
        shared.append((None,remainder,{str(space.id)}))
    return shared


def _construct_surface(element, zone, role, boundary, area, normal, owners, settings, defaults, suffix, partition_side=None,
                       radiation_geometry=()):
    label = f'{zone.id}/{element.Uid}/{role}{suffix}'
    glass = role in {'window','skylight'}
    sources = [('template', settings['_template_id']), ('template_table',settings['_template_source'])] if '_template_id' in settings else []
    if settings.get('_gbxml_source_sha256'):
        sources.append(('external_gbxml_sha256',settings['_gbxml_source_sha256']))
    def get(key, fallback, alias=None):
        if key in settings and settings[key] is not None:
            value, source = settings[key], ('template_equivalent:' + settings['_template_source']
                                           if key in settings.get('_template_fields',()) else 'explicit_or_model')
        elif alias and alias in settings and settings[alias] is not None:
            value, source = settings[alias], f'legacy_mapping:{alias}'
        else:
            value, source = fallback, 'default_assumption'
        sources.append((key, source))
        if key in settings.get('_gbxml_fields',()):
            sources[-1] = (key,'gbXML_source')
        return value
    layers = []
    for layer in settings.get('layers', ()):
        if 'resistance_m2k_w' in layer:
            r = layer['resistance_m2k_w']
            capacity = layer['capacity_j_m2k']
            thickness = layer.get('thickness_m', 0.)
        else:
            thickness = number(layer['thickness_m'], label + '/layer thickness', strictly_positive=True)
            r = thickness / number(layer['conductivity_w_mk'], label + '/conductivity', strictly_positive=True)
            capacity = thickness * number(layer['density_kg_m3'], label + '/density') * number(layer['specific_heat_j_kgk'], label + '/specific heat')
        layers.append(LayerRequest(float(r), float(capacity), float(thickness)))
    if layers:
        resistance = sum(l.resistance_m2k_w for l in layers)
        capacity = 0.
        sources.append(('layers','explicit_or_model'))
    else:
        if settings.get('resistance_m2k_w') is not None:
            resistance = number(get('resistance_m2k_w', 0),label+'/R',strictly_positive=True)
        else:
            u = number(get('u_value', zone.settings.get('zone_winU' if glass else 'zone_wallU', defaults.window_u_value if glass else defaults.wall_u_value)), label+'/U',strictly_positive=True)
            convention = get('u_convention','air_to_air')
            if convention not in {'air_to_air','surface_to_surface'}:
                raise ValueError(f'{label}: invalid u_convention')
            rsi = number(get('reference_inside_m2k_w', .17 if role=='floor' else .10 if role in {'roof','ceiling'} else .13),label+'/Rsi')
            rse = number(get('reference_outside_m2k_w', .04),label+'/Rse')
            resistance = 1 / u - (rsi + rse if convention=='air_to_air' else 0)
        if glass:
            capacity = 0.
            if settings.get('thermal_mass_j_m2k',0) != 0:
                raise ValueError(f'{label}: glass must have zero capacity')
        elif settings.get('thermal_mass_j_m2k') is not None:
            capacity = number(get('thermal_mass_j_m2k',0),label+'/capacity')
        elif settings.get('volumetric_heat_capacity') is not None and settings.get('thickness') is not None:
            capacity = number(get('volumetric_heat_capacity',0),label+'/volumetric capacity') * number(get('thickness',0),label+'/thickness',strictly_positive=True)
        else:
            capacity = get('thermal_mass_j_m2k', defaults.slab_capacity_j_m2k if role in {'floor','ceiling','roof'} else defaults.opaque_capacity_j_m2k)
    # Each owner receives the half construction adjacent to its room.
    scope = settings.get('construction_scope','full_element')
    if scope not in {'full_element','surface_side'}:
        raise ValueError(f'{label}: invalid construction_scope')
    if (len(owners) == 2 or partition_side is not None) and not glass and scope == 'full_element':
        if layers:
            side = partition_side if partition_side is not None else (0 if float(np.dot(np.asarray(element.normal), normal)) < 0 else 1)
            layers = split_layers(layers, side)
            resistance = sum(l.resistance_m2k_w for l in layers)
        else:
            resistance *= .5
            capacity *= .5
        sources.append(('shared_construction','midplane_adiabatic_half'))
    elif layers and scope == 'full_element' and np.dot(np.asarray(element.normal),normal)>0:
        # Full-element layers follow the source normal; native layers must run
        # from the exterior/back boundary to the particular room's inside face.
        layers = tuple(reversed(layers))
    outside = boundary == 'outdoors'
    sky = (1 + normal[2]) / 2 if outside else 0.
    return SurfaceRequest(label, str(element.Uid),str(zone.id),role,boundary,float(area),float(resistance),float(capacity),
        float(get('solar_reflectance',defaults.glass_solar_reflectance if glass else defaults.solar_reflectance)),
        float(get('solar_transmittance',defaults.solar_transmittance)) if glass else 0.,
        float(get('outside_emissivity',defaults.glass_emissivity if glass else defaults.opaque_emissivity,'emissivity')),
        float(get('inside_emissivity',defaults.glass_emissivity if glass else defaults.opaque_emissivity)),
        float(get('hi_w_m2k',defaults.inside_film_w_m2k)),
        float(get('ho_w_m2k',0.)) if outside else 0.,
        float(get('sky_view',sky)) if outside else 0.,
        float(get('ground_view',1-sky)) if outside else 0.,
        float(get('ground_contact_w_m2k',defaults.ground_contact_w_m2k)) if boundary=='ground' else 0.,
        float(get('absorbed_inside_fraction',0.)),tuple(layers),tuple(normal),tuple(sources),
        outside and settings.get('ho_w_m2k') is None,
        settings.get('_template_id',''),tuple(sorted(settings.get('_template_limits',{}).items())),
        tuple(sorted(settings.get('_template_context',{}).items())),radiation_geometry)


def _radiation_geometry(geometry, projection):
    def ring(points):
        return tuple(tuple(float(v) for v in shapely.get_coordinates(
            projection.toWorld(shapely.Point(float(p[0]),float(p[1]),0.)),include_z=True)[0]) for p in points)
    return tuple((ring(p.exterior.coords),tuple(ring(h.coords) for h in p.interiors))
                 for p in shapely.get_parts(geometry) if p.geom_type=='Polygon' and p.area>1e-10)


def _solar_vectors(weather, defaults):
    day = np.arange(8760) // 24 + 1
    clock = np.arange(8760) % 24 + .5
    angle = 2 * np.pi * (day-1) / 365
    equation = 229.18 * (.000075+.001868*np.cos(angle)-.032077*np.sin(angle)-.014615*np.cos(2*angle)-.040849*np.sin(2*angle))
    solar_hour = clock + (equation + 4*float(weather.location.longitude) - 60*defaults.time_zone_hours)/60
    hour_angle = np.radians(15*(solar_hour-12))
    declination = np.radians(23.45*np.sin(2*np.pi*(284+day)/365))
    lat = np.radians(float(weather.location.latitude))
    east = -np.cos(declination)*np.sin(hour_angle)
    north = np.cos(lat)*np.sin(declination)-np.sin(lat)*np.cos(declination)*np.cos(hour_angle)
    up = np.sin(lat)*np.sin(declination)+np.cos(lat)*np.cos(declination)*np.cos(hour_angle)
    return np.stack((east,north,up))


def _facade_window_ratios(spaces, owners, ground, overrides):
    """Public-building WWR: whole building, same outward facade normal.

    Include all rooms even when only one zone is simulated. Reuse the exact
    shared-boundary and ground clipping rules used by the surface adapter.
    """
    totals, room_totals = {}, {}
    for space in spaces.values():
        seen = set()
        for element in space.getAllFaces(to_dict=True).get('MoosasWall', ()):
            uid = str(element.Uid)
            if uid in seen: continue
            seen.add(uid)
            normal = np.asarray(element.normal,dtype=float)
            normal /= np.linalg.norm(normal)
            center = np.asarray(element.getWeightCenter(),dtype=float).reshape(-1)
            if np.dot(normal,center-np.asarray(space.getWeightCenter(),dtype=float).reshape(-1))<0:
                normal = -normal
            projection = Projection(Vector(center),Vector(normal))
            faces = [shapely.force_2d(projection.toUV(f)) for f in np.asarray(element.face).flatten()]
            footprint = shapely.union_all([shapely.Polygon(f.exterior) for f in faces])
            aperture = shapely.intersection(footprint,shapely.union_all([
                shapely.force_2d(projection.toUV(f)) for opening in element.glazingElement if opening.category!=2
                for f in np.asarray(opening.face).flatten()]))
            es = {**element.settings, **overrides.get(uid,{})}
            es.update(es.get('side_overrides',{}).get(str(space.id),{}))
            es.update(overrides.get(f'{space.id}/{uid}/wall',{}))
            boundary = es.get('boundary_condition')
            eo = owners[uid]
            if boundary is None and not element.isOuter and len(eo)==1: boundary='adiabatic'
            adjacency = _adjacency_parts(space,eo,spaces,footprint,projection,normal,center) if len(eo)>1 else ((None,footprint,eo),)
            for index,(inferred,patch,_) in enumerate(adjacency):
                bc = boundary or inferred
                parts = ((bc,patch),) if bc else _ground_parts(patch,projection,ground)
                for bc,part in parts:
                    if bc!='outdoors': continue
                    # Normative opening ratio includes the gross wall opening.
                    key = tuple(np.round(normal,6))
                    gross,glass = totals.get(key,(0.,0.))
                    opening_area = shapely.intersection(aperture,part).area
                    totals[key] = (gross+part.area,glass+opening_area)
                    room_key = (str(space.id), key)
                    gross,glass = room_totals.get(room_key,(0.,0.))
                    room_totals[room_key] = (gross+part.area,glass+opening_area)
    ratios = lambda values: {key: glass/gross for key,(gross,glass) in values.items() if gross>1e-9}
    return ratios(totals),ratios(room_totals)


def build_energy_input(model, weather, *, defaults=None, zone_ids=None, start_hour=0, hours=8760,
                       ground_level_m=None, surface_overrides=None, zone_overrides=None,
                       irradiance_by_surface=None, schedule_path=None, require_radiation=False,
                       core=None, temporal_scale=None, spatial_scale=None,
                       template_id=None, template_path=None, template_context=None,
                       template_overrides_model=False):
    """Build CES3 v2 requests; shared surfaces are resolved independently per owner.

    Override keys are element Uids or complete surface-side IDs. Legacy seasonal
    solar gain fields are not consumed by the dynamic surface model.
    """
    source_defaults = getattr(model,'energy_settings',{}).get('energy_defaults',{})
    defaults = defaults or EnergyDefaults(**source_defaults)
    for key,value in source_defaults.items():
        if getattr(defaults,key) != value:
            raise ValueError(f'gbXML source mismatch: EnergyDefaults/{key}')
    template_records = building_templates(template_path) if template_id is not None or template_path is not None else None
    if template_path is not None and template_id is None:
        raise ValueError('template_path requires template_id to select a template explicitly')
    if template_id is not None and (template_id not in template_records or '_thermal_template' not in template_records[template_id]):
        raise ValueError(f'Unknown structured thermal template {template_id!r}')
    if schedule_path:
        load_schedule(model, schedule_path)
    if int(start_hour)!=start_hour or int(hours)!=hours or hours<=0 or start_hour<0 or start_hour+hours>8760:
        raise ValueError('start_hour/hours must define a nonempty window within 0..8759')
    if require_radiation and not irradiance_by_surface:
        raise ValueError('CETUS requires hourly incident irradiance_by_surface; seasonal zone radiation cannot be reused')
    config = getattr(model,'energy_settings',{})
    surface_overrides = {**config.get('surface_overrides',{}), **(surface_overrides or {})}
    zone_overrides = {**config.get('zone_overrides',{}), **(zone_overrides or {})}
    from ...model.io.gbxml_energy import validate as validate_external_thermal_model
    validate_external_thermal_model(model,surface_overrides,zone_overrides,template_overrides_model)
    if ground_level_m is None:
        ground_level_m = config.get('ground_level_m', min(float(s.level) for s in model.spaceList))
    ground_level_m = number(ground_level_m,'ground_level_m',minimum=-10000)
    owners = {}
    owner_height_bounds = {}
    element_geometry = {}
    spaces = {str(space.id):space for space in list(model.spaceList)+list(model.voidList)}
    for space in spaces.values():
        for element in space.getAllFaces(to_dict=False):
            owners.setdefault(str(element.Uid),set()).add(str(space.id))
            owners[str(element.Uid)].update(str(v) for v in element.space)
    selected = {str(v) for v in zone_ids} if zone_ids is not None else {str(s.id) for s in model.spaceList}
    if selected - {str(s.id) for s in model.spaceList}:
        raise ValueError('Unknown zone_ids: ' + ', '.join(sorted(selected-{str(s.id) for s in model.spaceList})))
    facade_ratios = None
    sun = _solar_vectors(weather, defaults)
    ghi = np.maximum(np.asarray(weather.global_radiation),0)
    dhi = np.clip(np.asarray(weather.diffuse_radiation),0,ghi)
    direct = np.divide(ghi-dhi,np.maximum(sun[2],.05),out=np.zeros(8760),where=sun[2]>.05)
    requests, irradiation = [], []
    schedule_cache = {}
    notes = ['Regional conduction is adiabatic; inter-zone airflow is excluded.',
             'Unspecified optics/film/construction values use recorded EnergyDefaults assumptions.']
    if not irradiance_by_surface:
        notes.append('Incident solar uses an unshaded direct/diffuse weather projection; no geometric shading/reflections.')
    for space in sorted(model.spaceList,key=lambda s:str(s.id)):
        if str(space.id) not in selected:
            continue
        settings = dict(space.settings)
        if template_id is not None:
            explicit = set() if template_overrides_model else set(getattr(space,'explicit_settings',()))
            settings.update({k:v for k,v in template_records[template_id].items() if k not in explicit})
            settings['zone_template'] = template_id
        settings.update(zone_overrides.get(str(space.id),{}))
        profile = settings.get('_thermal_template')
        if profile and settings.get('zone_template') not in {profile['id'],profile.get('alias')}:
            settings.pop('_thermal_template')
            profile = None
        context = {**(profile or {}).get('context',{}), **(template_context or {})}
        if profile:
            if facade_ratios is None:
                facade_ratios = _facade_window_ratios(spaces,owners,ground_level_m,surface_overrides)
            if 'storeys' not in context:
                context['storeys'] = len({round(float(s.level),3) for s in model.spaceList if float(s.level)>=ground_level_m-1e-6})
            notes.extend((f"Template {profile['id']}: {profile['operation']['assumptions']}",
                          'Unknown storeys/shape/inertia use the stricter table column; U targets are equivalent constructions, not certified material recipes.'))
        def templated(element, values, role, boundary, normal, wwr, override_ids=()):
            if profile and role=='window':
                facade_key = tuple(np.round(normal,6))
                if settings.get('type') == 'RESIDENTIAL':
                    wwr = facade_ratios[1].get((str(space.id),facade_key),wwr)
                else:
                    wwr = facade_ratios[0].get(facade_key,wwr)
            if profile and role=='skylight' and boundary=='outdoors':
                maximum = profile['envelope'].get('maximum_skylight_ratio')
                if maximum is not None and wwr>maximum+1e-9:
                    notes.append('Roof glazing ratio exceeds prescriptive table applicability; this scenario does not establish compliance.')
            true_normal = np.asarray(normal,dtype=float).copy()
            theta = np.radians(defaults.north_angle_degrees)
            true_normal[:2] = [true_normal[0]*np.cos(theta)+true_normal[1]*np.sin(theta),-true_normal[0]*np.sin(theta)+true_normal[1]*np.cos(theta)]
            resolved, warnings = resolve_surface_template(settings,role,boundary,true_normal,wwr=context.get('window_wall_ratio',wwr),context=context)
            notes.extend(warnings)
            if not resolved: return values
            protected = set(values.get('side_overrides',{}).get(str(space.id),{}))
            protected.update(surface_overrides.get(str(element.Uid),{}))
            for key in override_ids:
                protected.update(surface_overrides.get(key,{}))
            return merge_surface_template(element,values,resolved,overrides_model=template_overrides_model,protected=protected)
        surfaces = []
        seen = set()
        room_center = np.asarray(space.getWeightCenter(),dtype=float).reshape(-1)
        groups = space.getAllFaces(to_dict=True)
        mass_ids = {str(e.Uid) for e in groups.get('InternalMass',())}
        for group, role in [('MoosasWall','wall'),('MoosasFloor','floor'),('MoosasCeiling','ceiling'),('InternalMass','wall')]:
            for element in groups.get(group,()):
                key = (str(element.Uid),role)
                if key in seen:
                    continue
                seen.add(key)
                eo = owners[str(element.Uid)]
                geometry_key=id(element)
                if geometry_key not in element_geometry:
                    element_geometry[geometry_key]=(np.asarray(element.normal,dtype=float).copy(),
                        np.asarray(element.getWeightCenter(),dtype=float).reshape(-1).copy())
                source_normal,center=element_geometry[geometry_key]
                normal = source_normal.copy()
                normal /= np.linalg.norm(normal)
                if role == 'floor': normal = np.array([0.,0.,-1.])
                elif role == 'ceiling': normal = np.array([0.,0.,1.])
                elif np.dot(normal,center-room_center)<0: normal=-normal
                projection = Projection(Vector(center),Vector(normal))
                uv = [shapely.force_2d(projection.toUV(face)) for face in np.asarray(element.face).flatten()]
                footprint = shapely.union_all([shapely.Polygon(p.exterior) for p in uv])
                openings = list(element.glazingElement)
                aperture = {}
                for opening in openings:
                    shapes = [shapely.force_2d(projection.toUV(f)) for f in np.asarray(opening.face).flatten()]
                    aperture[str(opening.Uid)] = shapely.intersection(footprint,shapely.union_all(shapes))
                # Preserve geometric holes even when no separate glazing/air
                # object has been attached; never manufacture opaque material.
                opaque = shapely.difference(shapely.union_all(uv),shapely.union_all(list(aperture.values())))
                host_wwr = float(shapely.union_all(list(aperture.values())).area / footprint.area) if footprint.area else 0.
                es = {**element.settings, **surface_overrides.get(str(element.Uid),{})}
                es.update(es.get('side_overrides',{}).get(str(space.id),{}))
                side_id = f'{space.id}/{element.Uid}/{role}'
                es.update(surface_overrides.get(side_id,{}))
                boundary = es.get('boundary_condition')
                if boundary is None and (str(element.Uid) in mass_ids or (not element.isOuter and len(eo)==1)):
                    boundary = 'adiabatic'
                adjacency = _adjacency_parts(space,eo,spaces,footprint,projection,normal,center,height_bounds=owner_height_bounds) if len(eo)>1 else ((None,footprint,eo),)
                parts=[]
                for inferred,patch,patch_owners in adjacency:
                    bc=boundary or inferred
                    if bc is not None:
                        parts.append((bc,patch,patch_owners))
                    elif role=='floor' and abs(center[2]-ground_level_m)<=1e-6:
                        parts.append(('ground',patch,patch_owners))
                    else:
                        parts.extend((bc,p,patch_owners) for bc,p in _ground_parts(patch,projection,ground_level_m))
                for index,(bc,patch,patch_owners) in enumerate(parts):
                    suffix = f'/patch{index}' if len(parts)>1 else ''
                    area = float(shapely.area(shapely.intersection(opaque,patch)))
                    if area>1e-7 and element.category != 2:
                        resolved_role = 'roof' if role=='ceiling' and bc=='outdoors' else role
                        side_settings = {**es, **surface_overrides.get(f'{space.id}/{element.Uid}/{resolved_role}{suffix}',{})}
                        opaque_boundary = side_settings.get('boundary_condition',bc)
                        side_settings = templated(element,side_settings,resolved_role,opaque_boundary,normal,host_wwr,
                            (side_id,f'{space.id}/{element.Uid}/{resolved_role}{suffix}'))
                        if str(element.Uid) in mass_ids and len(eo)==1:
                            for side in (0,1):
                                surfaces.append(_construct_surface(element,space,resolved_role,opaque_boundary,area,normal if side==0 else -normal,patch_owners,side_settings,defaults,suffix+f'/mass{side}',side))
                        else:
                            surfaces.append(_construct_surface(element,space,resolved_role,opaque_boundary,area,normal,patch_owners,side_settings,defaults,suffix,
                                radiation_geometry=_radiation_geometry(shapely.intersection(opaque,patch),projection)))
                    for opening in openings:
                        if opening.category==2:
                            continue
                        ga = float(shapely.area(shapely.intersection(aperture[str(opening.Uid)],patch)))
                        if ga<=1e-7:
                            continue
                        gs = {**opening.settings, **surface_overrides.get(str(opening.Uid),{})}
                        gs.update(gs.get('side_overrides',{}).get(str(space.id),{}))
                        gr = 'wall' if gs.get('opaque_opening') else 'window' if role=='wall' else 'skylight'
                        gs.update(surface_overrides.get(f'{space.id}/{opening.Uid}/{gr}',{}))
                        gs.update(surface_overrides.get(f'{space.id}/{opening.Uid}/{gr}{suffix}',{}))
                        gs = templated(opening,gs,gr,gs.get('boundary_condition',bc),normal,host_wwr,
                            (f'{space.id}/{opening.Uid}/{gr}',f'{space.id}/{opening.Uid}/{gr}{suffix}'))
                        surfaces.append(_construct_surface(opening,space,gr,gs.get('boundary_condition',bc),ga,normal,owners[str(opening.Uid)],gs,defaults,suffix,
                            radiation_geometry=_radiation_geometry(shapely.intersection(aperture[str(opening.Uid)],patch),projection)))
        surfaces.sort(key=lambda surface:surface.surface_id)
        if not surfaces:
            raise ValueError(f'{space.id}: no thermal surfaces after excluding air openings')
        template_type = str(settings.get('zone_template','')).split('_')[-1].upper()
        def program(key, fallback):
            value = settings.get(key, fallback)
            if value is None: value=fallback
            overridden = key in zone_overrides.get(str(space.id),{}) or (not template_overrides_model and key in getattr(space,'explicit_settings',()))
            absolute = template_program(settings,key,value) if not overridden else None
            if absolute is not None:
                value = absolute
            elif not profile and key in ('zone_ppsm','zone_popheat','zone_equipment','zone_lighting') and not isinstance(value,str) and not overridden:
                value = get_schedule_name(model,template_type,key) or value
            result = expand_schedule(model,value,f'{space.id}/{key}',schedule_cache)
            if key not in {'zone_h_temp','zone_c_temp'} and np.any(result<0):
                raise ValueError(f'{space.id}/{key}: schedule values must be nonnegative')
            return result
        area = number(settings.get('floor_area_m2',space.area),f'{space.id}/area',strictly_positive=True)
        volume = number(settings.get('volume_m3',area * space.height),f'{space.id}/volume',strictly_positive=True)
        density = program('zone_ppsm',defaults.occupant_density)
        people = program('zone_popheat',defaults.occupant_heat_w)
        equipment = program('zone_equipment',defaults.equipment_w_m2)*area
        lighting = program('zone_lighting',defaults.lighting_w_m2)*area
        heat = program('zone_h_temp',defaults.heating_setpoint_c)
        cool = program('zone_c_temp',defaults.cooling_setpoint_c)
        if np.any(heat>cool): raise ValueError(f'{space.id}: heating setpoint exceeds cooling')
        clock = np.arange(8760)%24
        start = number(settings.get('zone_work_start',0),f'{space.id}/work start')
        end = number(settings.get('zone_work_end',24),f'{space.id}/work end')
        if start>24 or end>24: raise ValueError(f'{space.id}: work hours outside 0..24')
        occupied = (clock>=start)&(clock<end) if start<end else (clock>=start)|(clock<end)
        ach = np.where(occupied,program('zone_infiltration',defaults.infiltration_ach),program('zone_nightACH',defaults.night_ach))
        vent = ach*volume + density*program('zone_pfav',defaults.fresh_air_m3_h_person)*area
        internal = density*people*area+equipment+lighting
        if np.any(internal<0) or np.any(vent<0) or np.any(lighting<0) or np.any(equipment<0):
            raise ValueError(f'{space.id}: negative internal load or ventilation')
        zone = ZoneRequest(str(space.id),str(settings.get('zone_name') or space.id),area,volume,tuple(surfaces),
            number(settings.get('zone_collingEER',defaults.cooling_cop),f'{space.id}/cooling COP',strictly_positive=True),
            number(settings.get('zone_HeatingEER',defaults.heating_cop),f'{space.id}/heating COP',strictly_positive=True),
            float(settings.get('initial_temperature_c',(heat[0]+cool[0])/2)),
            float(settings.get('internal_radiant_fraction',defaults.internal_radiant_fraction)),
            float(settings.get('solar_floor_fraction',defaults.solar_floor_fraction)),readonly(internal),readonly(lighting),readonly(equipment),
            readonly(heat),readonly(cool),readonly(vent),readonly(program('hvac_enabled',1. if space.conditioned else 0.)))
        rad = []
        for surface in surfaces:
            prescribed = (irradiance_by_surface or {}).get(surface.surface_id,(irradiance_by_surface or {}).get(surface.source_element_id))
            if prescribed is not None:
                values = np.asarray(prescribed,dtype=float)
                if values.shape!=(8760,) or not np.all(np.isfinite(values)) or np.any(values<0):
                    raise ValueError(f'{surface.surface_id}: irradiance requires 8760 nonnegative W/m2 values')
            elif surface.boundary_condition=='outdoors':
                if require_radiation: raise ValueError(f'{surface.surface_id}: missing hourly incident irradiance')
                normal = np.array(surface.normal)
                theta = np.radians(defaults.north_angle_degrees)
                normal[:2] = [normal[0]*np.cos(theta)+normal[1]*np.sin(theta),-normal[0]*np.sin(theta)+normal[1]*np.cos(theta)]
                values = direct*np.maximum(normal@sun,0)+dhi*surface.sky_view+ghi*defaults.ground_albedo*surface.ground_view
            else:
                values = np.zeros(8760)
            if surface.boundary_condition!='outdoors' and np.any(values):
                raise ValueError(f'{surface.surface_id}: non-outdoor boundary has incident external solar')
            rad.append(values)
        matrix = np.asarray(rad,dtype=float)
        matrix.setflags(write=False)
        requests.append(zone);irradiation.append(matrix)
    if not requests: raise ValueError('No zones selected')
    return EnergyRequest(tuple(requests),weather,tuple(irradiation),int(start_hour),int(hours),tuple(dict.fromkeys(notes)))
