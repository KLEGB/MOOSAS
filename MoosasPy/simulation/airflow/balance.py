"""Hourly 5R1C thermal balance used by the airflow simulation layer.

Temperatures are Celsius, heat rates W, conductances W/K, capacities J/K,
and CONTAM airflow matrices kg/s with the outdoor node last.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from math import isfinite

import numpy as np

from ...utils.constant import afn
from ..contracts import OCCUPANT_SENSIBLE_HEAT_W


@dataclass(frozen=True)
class BalanceParameters:
    air_density_kg_m3: float = 1.2
    air_specific_heat_j_kgk: float = 1000.0
    initial_temperature_c: float = afn.INITIAL_TEMPERATURE_C
    coupling_tolerance_c: float = afn.INTERZONE_TOLERANCE_C
    coupling_max_iterations: int = afn.INTERZONE_MAX_ITERATIONS
    mass_surface_heat_transfer_w_m2k: float = 9.1
    surface_air_heat_transfer_w_m2k: float = 3.45
    internal_gain_air_fraction: float = 0.5

    def __post_init__(self):
        for name, value in self.__dict__.items():
            if not isfinite(float(value)) or (name not in ('initial_temperature_c', 'internal_gain_air_fraction') and float(value) <= 0):
                raise ValueError(f'Invalid balance parameter {name}={value!r}')
        if not 0 <= self.internal_gain_air_fraction <= 1:
            raise ValueError('internal_gain_air_fraction must be in [0, 1]')


@dataclass(frozen=True)
class BalanceState:
    zone_ids: tuple[str, ...]
    ta_c: tuple[float, ...]
    ts_c: tuple[float, ...]
    tm_c: tuple[float, ...]


@dataclass(frozen=True)
class VentilationBoundary:
    """Lumped R1 terms: Q_ve = source_w - conductance_w_k * T_air."""
    conductance_w_k: dict[str, float]
    source_w: dict[str, float]

    def __post_init__(self):
        h = {str(k): float(v) for k, v in self.conductance_w_k.items()}
        s = {str(k): float(v) for k, v in self.source_w.items()}
        if h.keys() != s.keys() or any(not isfinite(v) or v < 0 for v in h.values()) \
                or any(not isfinite(v) for v in s.values()):
            raise ValueError('Invalid R1 boundary values or zone mapping')
        if any(h[z] == 0 and s[z] != 0 for z in h):
            raise ValueError('A zero R1 conductance requires a zero source')
        object.__setattr__(self, 'conductance_w_k', h)
        object.__setattr__(self, 'source_w', s)

    @classmethod
    def zero(cls, zone_ids):
        return cls(dict.fromkeys(zone_ids, 0.0), dict.fromkeys(zone_ids, 0.0))

    @classmethod
    def from_mass_flows(cls, incoming, cp_j_kgk=1000.0):
        if not isfinite(float(cp_j_kgk)) or cp_j_kgk <= 0:
            raise ValueError('cp_j_kgk must be positive')
        h, s = {}, {}
        for zone, streams in incoming.items():
            h[str(zone)] = s[str(zone)] = 0.0
            for mass_kg_s, supply_c in streams:
                conductance = float(cp_j_kgk) * float(mass_kg_s)
                if not isfinite(conductance) or conductance < 0 or not isfinite(float(supply_c)):
                    raise ValueError('Invalid mass flow or supply temperature')
                h[str(zone)] += conductance
                s[str(zone)] += conductance * float(supply_c)
        return cls(h, s)

    @classmethod
    def from_ach(cls, volumes_m3, ach, outdoor_temperature_c, rho_kg_m3=1.2, cp_j_kgk=1000.0):
        if isinstance(ach, dict) and set(ach) != set(volumes_m3):
            raise ValueError('ACH and zone volume mappings differ')
        incoming = {}
        for zone, volume in volumes_m3.items():
            value = ach[zone] if isinstance(ach, dict) else ach
            mass = float(rho_kg_m3) * float(volume) * float(value) / afn.TIME_STEP_SECONDS
            incoming[zone] = [(mass, outdoor_temperature_c)]
        return cls.from_mass_flows(incoming, cp_j_kgk)


@dataclass(frozen=True)
class AirflowThermalBoundary:
    """CONTAM directional mass-flow matrix; zones are ordered as zone_ids."""
    zone_ids: tuple[str, ...]
    mass_flow_kg_s: np.ndarray
    cp_j_kgk: float = 1000.0
    mass_balance_tolerance_kg_s: float = afn.MASS_BALANCE_TOLERANCE_KG_S

    def __post_init__(self):
        ids = tuple(map(str, self.zone_ids))
        matrix = np.asarray(self.mass_flow_kg_s, dtype=float).copy()
        if matrix.shape != (len(ids) + 1, len(ids) + 1):
            raise ValueError('AFN matrix must contain one outdoor node after all zones')
        if not np.isfinite(matrix).all() or np.any(matrix < -1e-12):
            raise ValueError('AFN mass flows must be finite and non-negative kg/s')
        matrix[matrix < 0] = 0
        if (len(set(ids)) != len(ids) or not isfinite(float(self.cp_j_kgk))
                or self.cp_j_kgk <= 0 or not isfinite(float(self.mass_balance_tolerance_kg_s))
                or self.mass_balance_tolerance_kg_s < 0):
            raise ValueError('Invalid AFN zone ordering or air specific heat')
        imbalance = matrix[:len(ids), :].sum(1) - matrix[:, :len(ids)].sum(0)
        if np.any(np.abs(imbalance) > self.mass_balance_tolerance_kg_s
                  + 1e-5 * np.maximum(matrix[:len(ids), :].sum(1), matrix[:, :len(ids)].sum(0))):
            raise ValueError(f'AFN matrix violates zone mass balance: max residual {np.max(np.abs(imbalance)):.6g} kg/s')
        object.__setattr__(self, 'zone_ids', ids)
        object.__setattr__(self, 'mass_flow_kg_s', matrix)

    @classmethod
    def from_mass_matrix(cls, zone_ids, mass_flow_kg_s, cp_j_kgk=1000.0,
                         mass_balance_tolerance_kg_s=1e-4):
        return cls(tuple(zone_ids), mass_flow_kg_s, cp_j_kgk, mass_balance_tolerance_kg_s)

    @property
    def conductance_w_k(self):
        n = len(self.zone_ids)
        return {z: float(self.cp_j_kgk * self.mass_flow_kg_s[i].sum()) for i, z in enumerate(self.zone_ids)}

    def source_w(self, outdoor_temperature_c, zone_temperatures_c):
        n = len(self.zone_ids)
        ta = np.asarray(zone_temperatures_c, dtype=float)
        if ta.shape != (n,) or not np.isfinite(ta).all():
            raise ValueError('AFN neighbor temperatures do not match zone order')
        q = self.cp_j_kgk * self.mass_flow_kg_s[:n, :n].T @ ta
        q += self.cp_j_kgk * self.mass_flow_kg_s[n, :n] * float(outdoor_temperature_c)
        return dict(zip(self.zone_ids, map(float, q)))

    def outgoing_m3_h(self, rho_kg_m3=1.2):
        n = len(self.zone_ids)
        return {z: float(self.mass_flow_kg_s[i].sum() * afn.TIME_STEP_SECONDS / rho_kg_m3) for i, z in enumerate(self.zone_ids)}


class BalanceModel:
    """Model-driven 5R1C adapter. It does not read RDF or mutate the model."""

    @classmethod
    def from_model(cls, model, network_dict, parameters=None, gain_resolver=None):
        self = cls()
        self.model = model
        self.network_dict = network_dict
        self.parameters = parameters if isinstance(parameters, BalanceParameters) else BalanceParameters(**(parameters or {}))
        spaces = {str(s.id): s for s in model.spaceList}
        if len(spaces) != len(model.spaceList):
            raise ValueError('Model contains duplicate space IDs')
        zones = list(network_dict.get('zones', {}).values())
        mapping = sorted(((int(z['prjIndex']), str(z.get('userName', z.get('id')))) for z in zones))
        if not mapping or len({p for p, _ in mapping}) != len(mapping) or any(n not in spaces for _, n in mapping):
            raise ValueError('Zone prjIndex/userName mapping is incomplete or duplicated')
        self.zone_ids = tuple(n for _, n in mapping)
        self.index = {name: i for i, name in enumerate(self.zone_ids)}
        n = len(mapping)
        self.area = np.array([float(spaces[z].area) for z in self.zone_ids])
        self.volume = np.array([self.area[i] * float(spaces[z].height) for i, z in enumerate(self.zone_ids)])
        self.cm = np.zeros(n)
        self.am = np.zeros(n)
        self.at = np.zeros(n)
        self.hem = np.zeros(n)
        self.hw = np.zeros(n)
        self.solar_area = np.zeros(n)
        self.coupling = np.zeros((n, n))
        surfaces = {}
        for name, space in spaces.items():
            groups = space.getAllFaces(to_dict=True)
            zone_index = self.index.get(name)
            if zone_index is not None:
                walls = list(groups.get('MoosasWall', ()))
                # ISO 13790 effective mass area requested for Moosas: gross
                # wall area plus the room's net floor area.
                self.am[zone_index] = sum(max(0.0, float(face.area)) for face in walls) + self.area[zone_index]
                for role in ('MoosasWall', 'MoosasFloor', 'MoosasCeiling', 'InternalMass'):
                    for face in groups.get(role, ()):
                        net_area = self._opaque_net_area(face)
                        self.cm[zone_index] += net_area * self._element_float(
                            face, 'thickness', 0.2) * self._element_float(
                            face, 'volumetric_heat_capacity', 2116000.0)
                        self.at[zone_index] += net_area
                for role in ('MoosasGlazing', 'MoosasSkylight'):
                    self.at[zone_index] += sum(max(0.0, float(face.area)) for face in groups.get(role, ()))
            for role in ('MoosasWall', 'MoosasFloor', 'MoosasCeiling', 'MoosasGlazing', 'MoosasSkylight'):
                for face in groups.get(role, ()):
                    uid = str(face.Uid)
                    record = surfaces.setdefault(uid, {'face': face, 'owners': set(), 'role': role})
                    record['owners'].add(name)
        self.diagnostics = []
        for item in surfaces.values():
            face, owners = item['face'], item['owners']
            selected = sorted(owners.intersection(self.index))
            if not selected:
                continue
            is_window = item['role'] in ('MoosasGlazing', 'MoosasSkylight') and getattr(face, 'category', 0) != 2
            area = (max(0.0, float(face.area)) if is_window else self._opaque_net_area(face))
            u = max(0.0, float(face.U_Value))
            ua = area * u
            if len(owners) > 1:
                if len(selected) != len(owners):
                    self.diagnostics.append(f"{face.Uid}: shared face spans outside selected airflow zones")
                    continue
                for a, b in combinations(selected, 2):
                    i, j = self.index[a], self.index[b]
                    self.coupling[i, j] += ua
                    self.coupling[j, i] += ua
            else:
                i = self.index[selected[0]]
                if is_window:
                    self.hw[i] += ua
                    self.solar_area[i] += area * max(0.0, float(face.SHGC))
                elif item['role'] != 'MoosasWall' or bool(getattr(face, 'isOuter', False)):
                    self.hem[i] += ua
        if np.any(self.cm <= 0) or np.any(self.am <= 0) or np.any(self.at <= 0):
            invalid = [self.zone_ids[i] for i in range(n)
                       if self.cm[i] <= 0 or self.am[i] <= 0 or self.at[i] <= 0]
            raise ValueError('5R1C requires positive actual thermal areas and capacities: ' + ', '.join(invalid))
        self.programs = {}
        self.model = model
        from ...model.building_templates import space_template, resolve_zone_settings
        for key, zone in network_dict.get('zones', {}).items():
            name = str(zone.get('userName', key))
            settings = resolve_zone_settings(spaces[name], space_template(model, spaces[name]))
            self.programs[name] = {**settings, **zone}
        self.gain_resolver = gain_resolver
        self.prj_order = tuple(p for p, _ in mapping)
        return self

    @staticmethod
    def _element_float(element, key, default):
        try:
            value = float(getattr(element, 'settings', {}).get(key, default))
        except (TypeError, ValueError):
            return float(default)
        return value if isfinite(value) and value > 0 else float(default)

    @staticmethod
    def _opaque_net_area(element):
        """Area of an opaque interior face excluding transparent sub-elements."""
        gross = max(0.0, float(element.area))
        openings = sum(max(0.0, float(opening.area))
                       for opening in getattr(element, 'glazingElement', ()))
        return max(0.0, gross - openings)

    @staticmethod
    def input_signature(model, network, parameters=None):
        """Detect in-place physical/program edits, excluding hourly derived loads."""
        import hashlib
        import json
        fields = ('userName', 'prjIndex', 'volume', 'zone_ppsm',
                  'zone_equipment', 'zone_lighting', 'zone_h_temp', 'zone_c_temp')
        zones = {str(k): {f: z.get(f) for f in fields}
                 for k, z in network.get('zones', {}).items()}
        spaces, surfaces = [], {}
        for space in model.spaceList:
            spaces.append((str(space.id), float(space.area), float(space.height),
                           {f: space.settings.get(f) for f in fields}))
            for role, faces in space.getAllFaces(to_dict=True).items():
                for face in faces:
                    surfaces[(str(space.id), role, str(face.Uid))] = (
                        float(face.area), float(face.U_Value),
                        float(getattr(face, 'SHGC', 0)), bool(getattr(face, 'isOuter', False)),
                        getattr(face, 'settings', {}).get('thickness'),
                        getattr(face, 'settings', {}).get('volumetric_heat_capacity'))
        payload = [spaces, sorted(surfaces.items()), zones, network.get('paths', {}),
                   parameters or {}, network.get('thermal', {})]
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()

    def initial_state(self, temperature_c=None):
        value = self.parameters.initial_temperature_c if temperature_c is None else float(temperature_c)
        vals = tuple([value] * len(self.zone_ids))
        return BalanceState(self.zone_ids, vals, vals, vals)

    def gains(self, hoy, internal_w=None, solar_w=None, *, internal=True):
        if internal_w is None:
            internal_w = {}
            for z, i in self.index.items():
                values = {}
                for field in ('zone_ppsm', 'zone_lighting', 'zone_equipment'):
                    values[field] = (0.0 if not internal or self.programs[z].get(field) == 'ALL_ZERO'
                                     else self._program_value(z, field, hoy, 0.0))
                internal_w[z] = self.area[i] * (
                    values['zone_ppsm'] * OCCUPANT_SENSIBLE_HEAT_W
                    + values['zone_lighting'] + values['zone_equipment'])
        solar_w = solar_w or {}
        return {z: {'internal_w': float(internal_w.get(z, 0.0)), 'solar_w': float(solar_w.get(z, 0.0))}
                for z in self.zone_ids}

    @staticmethod
    def _zone(cm, am, at, hem, hw, hve, source, g_row, neighbor, tm_prev, tout, gains, solar,
              h_ms_w_m2k, h_is_w_m2k, internal_gain_air_fraction, hvac=0.0):
        hms, his = h_ms_w_m2k * am, h_is_w_m2k * at
        hb = hw + g_row.sum()
        hve = max(hve, 1e-9)
        h1 = 1 / (1 / hve + 1 / his)
        h2 = h1 + hb
        h3 = 1 / (1 / max(h2, 1e-9) + 1 / hms)
        boundary = hw * tout + float(g_row @ neighbor)
        phi_ia = internal_gain_air_fraction * gains + hvac
        shared = (1.0 - internal_gain_air_fraction) * gains + solar
        phi_m = am / at * shared
        phi_st = (1 - am / at - hw / (h_ms_w_m2k * at)) * shared
        air_source = (phi_ia + source) / hve
        phi_m_tot = phi_m + hem * tout + h3 * (phi_st + boundary + h1 * air_source) / h2
        tm_next = (tm_prev * (cm / afn.TIME_STEP_SECONDS - 0.5 * (h3 + hem)) + phi_m_tot) / (cm / afn.TIME_STEP_SECONDS + 0.5 * (h3 + hem))
        tm = 0.5 * (tm_next + tm_prev)
        ts = (hms * tm + phi_st + boundary + h1 * air_source) / (hms + hb + h1)
        ta = (his * ts + source + phi_ia) / (his + hve)
        return tm_next, ta, ts

    def _program_value(self, zone, field, hoy, default):
        value = self.programs.get(zone, {}).get(field, default)
        try:
            return float(value)
        except (TypeError, ValueError):
            if self.gain_resolver is None:
                from ...model.building_templates import expand_schedule
                return float(expand_schedule(
                    self.model, value, f'{zone}/{field}', allow_nan=field in {'zone_h_temp','zone_c_temp'})[hoy])
            return float(self.gain_resolver(value, hoy, field, zone))

    def step(self, state, hoy, outdoor_temperature_c, gains, ventilation, ideal_hvac=True):
        if tuple(state.zone_ids) != self.zone_ids:
            raise ValueError('Balance state zone ordering differs')
        if isinstance(ventilation, AirflowThermalBoundary):
            if tuple(ventilation.zone_ids) != self.zone_ids:
                raise ValueError('AFN zone ordering differs from thermal state')
        elif not isinstance(ventilation, VentilationBoundary) or set(ventilation.conductance_w_k) != set(self.zone_ids):
            raise ValueError('R1 boundary zones differ from thermal state')
        p = self.parameters
        if int(hoy) != hoy or not 0 <= int(hoy) <= 8759:
            raise ValueError('hoy must be an integer in [0, 8759]')
        neighbor = np.asarray(state.ta_c, dtype=float)
        for iteration in range(1, p.coupling_max_iterations + 1):
            source = (ventilation.source_w(outdoor_temperature_c, neighbor)
                      if isinstance(ventilation, AirflowThermalBoundary) else ventilation.source_w)
            tm, ta, ts = (np.empty(len(self.zone_ids)) for _ in range(3))
            for z, i in self.index.items():
                q = gains[z]
                args = (
                    self.cm[i], self.am[i], self.at[i], self.hem[i], self.hw[i],
                    ventilation.conductance_w_k[z], source[z], self.coupling[i], neighbor,
                    state.tm_c[i], outdoor_temperature_c, float(q.get('internal_w', 0)),
                    float(q.get('solar_w', 0)), p.mass_surface_heat_transfer_w_m2k,
                    p.surface_air_heat_transfer_w_m2k, p.internal_gain_air_fraction)
                tm[i], ta[i], ts[i] = self._zone(*args)
                program = self.programs.get(z, {})
                if ideal_hvac:
                    setpoint = None
                    heat = self._program_value(z, 'zone_h_temp', hoy, float('nan'))
                    cool = self._program_value(z, 'zone_c_temp', hoy, float('nan'))
                    heat_available = self._program_value(z, 'zone_heating_availability', hoy, 0.) > 0
                    cool_available = self._program_value(z, 'zone_cooling_availability', hoy, 0.) > 0
                    if heat_available and np.isfinite(heat) and ta[i] < heat:
                        setpoint = heat
                    elif cool_available and np.isfinite(cool) and ta[i] > cool:
                        setpoint = cool
                    if setpoint is not None:
                        probe = self._zone(*args, 10.0 * self.area[i])
                        if abs(probe[1] - ta[i]) < 1e-12:
                            raise ValueError(f'Degenerate ideal HVAC response in zone {z}')
                        required = 10.0 * self.area[i] * (setpoint - ta[i]) / (probe[1] - ta[i])
                        tm[i], ta[i], ts[i] = self._zone(*args, required)
            if not np.isfinite([tm, ta, ts]).all():
                raise ValueError('5R1C produced non-finite temperatures')
            if np.max(np.abs(ta - neighbor)) <= p.coupling_tolerance_c:
                return BalanceState(self.zone_ids, tuple(ta), tuple(ts), tuple(tm)), iteration
            neighbor = 0.5 * neighbor + 0.5 * ta
        raise RuntimeError('5R1C inter-zone iteration did not converge')
