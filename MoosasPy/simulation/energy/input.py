"""SI input contracts and model-to-surface resolution for MOOSAS Energy."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from hashlib import sha256
import json
import math

import numpy as np
import shapely

from ...model.thermal_templates import (
    building_templates, resolve_surface_template, merge_surface_template,
    template_program, validate_template_snapshot,
)
from ...transform.geometry.geos import Projection, Vector


@dataclass(frozen=True)
class EnergyDefaults:
    """Engineering assumptions used only where model values are absent."""
    wall_u_value: float = 0.5
    window_u_value: float = 2.4
    opaque_capacity_j_m2k: float = 60000.
    slab_capacity_j_m2k: float = 120000.
    inside_film_w_m2k: float = 3.
    opaque_emissivity: float = .9
    glass_emissivity: float = .84
    solar_reflectance: float = .3
    glass_solar_reflectance: float = .075
    solar_transmittance: float = .6
    ground_contact_w_m2k: float = 100.
    internal_radiant_fraction: float = .6
    solar_floor_fraction: float = .642
    ground_albedo: float = .2
    time_zone_hours: float = 8.
    north_angle_degrees: float = 0.
    cooling_cop: float = 2.8
    heating_cop: float = 2.2
    heating_setpoint_c: float = 20.
    cooling_setpoint_c: float = 26.
    occupant_density: float = .033
    occupant_heat_w: float = 75.
    fresh_air_m3_h_person: float = 30.
    equipment_w_m2: float = 4.3
    lighting_w_m2: float = 5.
    infiltration_ach: float = .5
    night_ach: float = .5

    def __post_init__(self):
        for key, value in asdict(self).items():
            if not math.isfinite(value):
                raise ValueError(f'EnergyDefaults.{key} must be finite')
        for key in ('wall_u_value','window_u_value','inside_film_w_m2k','ground_contact_w_m2k','cooling_cop','heating_cop'):
            if getattr(self,key)<=0:
                raise ValueError(f'EnergyDefaults.{key} must be positive')
        for key in ('opaque_capacity_j_m2k','slab_capacity_j_m2k','occupant_density','occupant_heat_w','fresh_air_m3_h_person','equipment_w_m2','lighting_w_m2','infiltration_ach','night_ach'):
            if getattr(self,key)<0:
                raise ValueError(f'EnergyDefaults.{key} must be nonnegative')
        for key in ('opaque_emissivity','glass_emissivity','solar_reflectance','glass_solar_reflectance','solar_transmittance','internal_radiant_fraction','solar_floor_fraction','ground_albedo'):
            if not 0<=getattr(self,key)<=1:
                raise ValueError(f'EnergyDefaults.{key} must be in 0..1')


@dataclass(frozen=True)
class LayerRequest:
    resistance_m2k_w: float
    capacity_j_m2k: float
    thickness_m: float = 0.

    def __post_init__(self):
        if (not math.isfinite(self.resistance_m2k_w) or self.resistance_m2k_w <= 0
                or not math.isfinite(self.capacity_j_m2k) or self.capacity_j_m2k < 0
                or not math.isfinite(self.thickness_m) or self.thickness_m < 0):
            raise ValueError('Layers require positive R and nonnegative capacity/thickness')


@dataclass(frozen=True)
class SurfaceRequest:
    surface_id: str
    source_element_id: str
    zone_id: str
    role: str
    boundary_condition: str
    area_m2: float
    resistance_m2k_w: float
    thermal_mass_j_m2k: float
    reflectance: float
    transmittance: float
    emissivity: float
    inside_emissivity: float
    hi_w_m2k: float
    ho_w_m2k: float
    sky_view: float
    ground_view: float
    ground_contact_w_m2k: float
    absorbed_inside_fraction: float = 0.
    layers: tuple[LayerRequest, ...] = ()
    normal: tuple[float, float, float] = (0., 0., 1.)
    sources: tuple[tuple[str, str], ...] = ()
    wind_driven_film: bool = True
    template_id: str = ''
    template_limits: tuple[tuple[str, float], ...] = ()
    template_context: tuple[tuple[str, str], ...] = ()
    radiation_geometry: tuple = ()  # world-space (exterior ring, holes), never on the wire

    def __post_init__(self):
        label = f'{self.zone_id}/{self.surface_id}'
        if self.role not in {'wall','floor','ceiling','roof','window','skylight'}:
            raise ValueError(f'{label}: invalid surface role')
        if self.boundary_condition not in {'outdoors','ground','adiabatic'}:
            raise ValueError(f'{label}: invalid boundary_condition')
        positive = ('area_m2','resistance_m2k_w','hi_w_m2k')
        nonnegative = ('thermal_mass_j_m2k','ho_w_m2k','ground_contact_w_m2k')
        fractions = ('reflectance','transmittance','emissivity','inside_emissivity',
                     'sky_view','ground_view','absorbed_inside_fraction')
        for key in positive + nonnegative + fractions:
            value = getattr(self, key)
            if not math.isfinite(value) or value < 0 or (key in positive and value <= 0) or (key in fractions and value > 1):
                raise ValueError(f'{label}: invalid {key}: {value}')
        if self.reflectance + self.transmittance > 1 + 1e-12:
            raise ValueError(f'{label}: solar reflectance + transmittance exceeds 1')
        glass = self.role in {'window','skylight'}
        if glass and (self.thermal_mass_j_m2k or self.layers):
            raise ValueError(f'{label}: glass must have zero thermal capacity')
        if not glass and self.transmittance:
            raise ValueError(f'{label}: opaque surface cannot transmit shortwave')
        if self.boundary_condition == 'outdoors':
            if abs(self.sky_view + self.ground_view - 1) > 1e-10 or self.ground_contact_w_m2k:
                raise ValueError(f'{label}: invalid outdoor view/contact boundary')
        elif self.ho_w_m2k or self.sky_view or self.ground_view:
            raise ValueError(f'{label}: non-outdoor surface has exterior film/view factors')
        if self.boundary_condition == 'adiabatic' and self.ground_contact_w_m2k:
            raise ValueError(f'{label}: adiabatic surface cannot contact ground')
        if self.boundary_condition == 'ground' and (glass or self.ground_contact_w_m2k <= 0):
            raise ValueError(f'{label}: ground boundary requires opaque contact')

    def native_model(self):
        values = asdict(self)
        for key in ('surface_id','zone_id','role','normal','sources','wind_driven_film','template_id','template_limits','template_context','radiation_geometry'):
            values.pop(key)
        values['name'] = self.surface_id
        values['kind'] = self.role
        values['layers'] = [dict(resistance_m2k_w=l.resistance_m2k_w, capacity_j_m2k=l.capacity_j_m2k) for l in self.layers]
        return values


def readonly(values):
    result = np.array(values, dtype=float, copy=True)
    if result.ndim != 1 or not np.all(np.isfinite(result)):
        raise ValueError('Hourly inputs must be finite one-dimensional arrays')
    result.setflags(write=False)
    return result


@dataclass(frozen=True)
class ZoneRequest:
    zone_id: str
    name: str
    area_m2: float
    volume_m3: float
    surfaces: tuple[SurfaceRequest, ...]
    cooling_cop: float
    heating_cop: float
    initial_c: float
    internal_radiant_fraction: float
    solar_floor_fraction: float
    internal_w: np.ndarray
    lighting_w: np.ndarray
    equipment_w: np.ndarray
    heat_setpoint_c: np.ndarray
    cool_setpoint_c: np.ndarray
    ventilation_m3_h: np.ndarray
    hvac_enabled: np.ndarray

    def __post_init__(self):
        if not self.zone_id or not self.surfaces or len({s.surface_id for s in self.surfaces})!=len(self.surfaces):
            raise ValueError(f'{self.zone_id}: empty/duplicate thermal surfaces')
        for key in ('area_m2','volume_m3','cooling_cop','heating_cop'):
            if not math.isfinite(getattr(self,key)) or getattr(self,key)<=0:
                raise ValueError(f'{self.zone_id}: invalid {key}')
        if not math.isfinite(self.initial_c) or self.initial_c<=-273.15:
            raise ValueError(f'{self.zone_id}: invalid initial_c')
        for key in ('internal_radiant_fraction','solar_floor_fraction'):
            if not 0<=getattr(self,key)<=1:
                raise ValueError(f'{self.zone_id}: {key} must be in 0..1')
        for key in ('internal_w','lighting_w','equipment_w','heat_setpoint_c','cool_setpoint_c','ventilation_m3_h','hvac_enabled'):
            value = readonly(getattr(self,key))
            if value.shape!=(8760,):
                raise ValueError(f'{self.zone_id}/{key}: expected 8760 values')
            if key not in {'heat_setpoint_c','cool_setpoint_c'} and np.any(value<0):
                raise ValueError(f'{self.zone_id}/{key}: negative value')
            object.__setattr__(self,key,value)
        if np.any(self.heat_setpoint_c>self.cool_setpoint_c):
            raise ValueError(f'{self.zone_id}: heating setpoint exceeds cooling')
        if not np.all(np.isin(self.hvac_enabled,(0,1))):
            raise ValueError(f'{self.zone_id}: hvac_enabled requires 0 or 1')

    def native_model(self):
        return dict(zone_id=self.zone_id, dt_s=3600., initial_c=self.initial_c,
                    cooling_cop=self.cooling_cop, heating_cop=self.heating_cop,
                    internal_radiant_fraction=self.internal_radiant_fraction,
                    solar_floor_fraction=self.solar_floor_fraction,
                    surfaces=[s.native_model() for s in self.surfaces])


@dataclass(frozen=True)
class EnergyRequest:
    zones: tuple[ZoneRequest, ...]
    weather: object
    irradiance: tuple[np.ndarray, ...]
    start_hour: int = 0
    hours: int = 8760
    warnings: tuple[str, ...] = ()

    def __post_init__(self):
        if not self.zones or len({z.zone_id for z in self.zones})!=len(self.zones):
            raise ValueError('Empty or duplicate zone IDs')
        if int(self.start_hour)!=self.start_hour or int(self.hours)!=self.hours or self.start_hour<0 or self.hours<1 or self.start_hour+self.hours>8760:
            raise ValueError('Invalid request hour window')
        if len(self.irradiance)!=len(self.zones):
            raise ValueError('Irradiance zone count differs')
        for zone,values in zip(self.zones,self.irradiance):
            if values.shape!=(len(zone.surfaces),8760) or not np.all(np.isfinite(values)) or np.any(values<0):
                raise ValueError(f'{zone.zone_id}: invalid hourly irradiance matrix')
        for key in ('temperature','ground_temperature','sky_temperature','pressure','wind_speed'):
            values=np.asarray(getattr(self.weather,key))
            if values.shape!=(8760,) or not np.all(np.isfinite(values)):
                raise ValueError(f'weather.{key}: requires 8760 finite values')
        if np.any(self.weather.pressure<=0) or np.any(self.weather.sky_temperature<=0) or np.any(self.weather.wind_speed<0):
            raise ValueError('Weather requires positive pressure, Kelvin sky temperature, and nonnegative wind')

    @property
    def signature(self):
        digest = sha256(json.dumps([z.native_model() for z in self.zones], sort_keys=True, allow_nan=False).encode())
        for zone in self.zones:
            digest.update(bytes(int(s.wind_driven_film) for s in zone.surfaces))
            for key in ('internal_w','lighting_w','equipment_w','heat_setpoint_c','cool_setpoint_c','ventilation_m3_h','hvac_enabled'):
                digest.update(np.asarray(getattr(zone,key), dtype='<f8').tobytes())
        return digest.hexdigest()

    def hour(self, relative_hour):
        h = self.start_hour + relative_hour
        w = self.weather
        # The MOOSAS/DeST CSV and WeatherData.sky_temperature are Kelvin.
        sky_ir = 5.670374419e-8 * float(w.sky_temperature[h]) ** 4
        ta = float(w.temperature[h])
        density = float(w.pressure[h]) / (287.05 * (ta + 273.15))
        rows = []
        for zone, irradiance in zip(self.zones, self.irradiance):
            films = [5.8 + 4.1 * float(w.wind_speed[h]) if s.boundary_condition == 'outdoors' and s.wind_driven_film else s.ho_w_m2k for s in zone.surfaces]
            rows.append(dict(air_c=ta, ground_c=float(w.ground_temperature[h]), sky_ir_w_m2=sky_ir,
                internal_w=float(zone.internal_w[h]), heat_setpoint_c=float(zone.heat_setpoint_c[h]),
                cool_setpoint_c=float(zone.cool_setpoint_c[h]),
                ventilation_w_k=density * 1006 * float(zone.ventilation_m3_h[h]) / 3600,
                irradiance_w_m2=irradiance[:, h].tolist(), ho_w_m2k=films,
                hvac_enabled=bool(zone.hvac_enabled[h])))
        return rows


@dataclass(frozen=True)
class EnergyState:
    model_signature: str
    next_hour: int
    zone_ids: tuple[str, ...]
    states: tuple[tuple[float, ...], ...]


# Model geometry, programs and hourly forcing resolution.

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
    from ...transform.geometry.polygon import planar_surface
    return planar_surface(geometry)


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
                       irradiance_by_surface=None, require_radiation=False,
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
    if int(start_hour)!=start_hour or int(hours)!=hours or hours<=0 or start_hour<0 or start_hour+hours>8760:
        raise ValueError('start_hour/hours must define a nonempty window within 0..8759')
    if require_radiation and not irradiance_by_surface:
        raise ValueError('MOOSAS Energy requires hourly incident irradiance_by_surface; seasonal zone radiation cannot be reused')
    config = getattr(model,'energy_settings',{})
    surface_overrides = {**config.get('surface_overrides',{}), **(surface_overrides or {})}
    zone_overrides = {**config.get('zone_overrides',{}), **(zone_overrides or {})}
    from ...model.io.gbxml.energy import validate as validate_external_thermal_model
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
        validate_template_snapshot(settings)
        profile = settings.get('_thermal_template')
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
                if profile['wwr_scope'] == 'zone':
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
                owner_normal=element.settings.get('geometry_owner_normals',{}).get(str(space.id))
                if owner_normal is not None:normal=np.asarray(owner_normal,dtype=float)
                elif role=='floor':
                    if normal[2]>0:normal=-normal
                elif role=='ceiling' and abs(normal[2])>1e-8:
                    if normal[2]<0:normal=-normal
                else:
                    region=space.force_2d();direction=normal[:2]
                    before=region.contains(shapely.Point(center[:2]-direction*.001))
                    after=region.contains(shapely.Point(center[:2]+direction*.001))
                    if after and not before:normal=-normal
                    elif before==after and np.dot(normal,center-room_center)<0:normal=-normal
                projection = Projection(Vector(center),Vector(normal))
                from ...transform.geometry.polygon import planar_surface
                uv=[p for face in np.asarray(element.face).flatten() for p in shapely.get_parts(planar_surface(projection.toUV(face)))]
                footprint = shapely.union_all([shapely.Polygon(p.exterior) for p in uv])
                openings = list(element.glazingElement)
                aperture = {}
                for opening in openings:
                    shapes=[planar_surface(projection.toUV(f)) for f in np.asarray(opening.face).flatten()]
                    aperture[str(opening.Uid)]=shapely.intersection(shapely.normalize(footprint),shapely.normalize(shapely.union_all(shapes)))
                # Preserve geometric holes even when no separate glazing/air
                # object has been attached; never manufacture opaque material.
                opaque=shapely.difference(shapely.normalize(shapely.union_all(uv)),shapely.normalize(shapely.union_all(list(aperture.values()))))
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
        def program(key, fallback):
            value = settings.get(key, fallback)
            if value is None: value=fallback
            overridden = key in zone_overrides.get(str(space.id),{}) or (not template_overrides_model and key in getattr(space,'explicit_settings',()))
            absolute = template_program(settings,key,value) if not overridden else None
            if absolute is not None:
                value = absolute
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
