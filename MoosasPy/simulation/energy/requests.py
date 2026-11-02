"""Explicit SI contracts for independent CETUS thermal zones."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from hashlib import sha256
import json
import math

import numpy as np


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
