"""CETUS CES3 v2 codec, persistent executable session and energy results."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import platform
import queue
import struct
import subprocess
import threading

import numpy as np

from ..contracts import SimulationResult
from ..engine import SubprocessEngine
from ..runner import CommandResult
from .input import EnergyRequest, EnergyState, build_energy_input


HEADER = struct.Struct('<4sHHQQ')
MAX_FRAME = 256 << 20
BOUNDARIES = {'outdoors': 0, 'ground': 1, 'adiabatic': 2}
MODEL_FIELDS = ('dt_s', 'initial_c', 'cooling_cop', 'heating_cop',
                'internal_radiant_fraction', 'solar_floor_fraction')
SURFACE_FIELDS = ('area_m2', 'resistance_m2k_w', 'thermal_mass_j_m2k',
                  'reflectance', 'transmittance', 'emissivity', 'hi_w_m2k',
                  'ho_w_m2k', 'sky_view', 'ground_view', 'ground_contact_w_m2k',
                  'absorbed_inside_fraction', 'inside_emissivity')
HOUR_FIELDS = ('air_c', 'ground_c', 'sky_ir_w_m2', 'internal_w',
               'heat_setpoint_c', 'cool_setpoint_c', 'ventilation_w_k',
               'additional_transmitted_w', 'additional_glass_inward_w')
HOUR_ARRAYS = ('irradiance_w_m2', 'ho_w_m2k', 'network_ir_w_m2')
OUTPUT_FIELDS = ('radiant_c', 'longwave_to_network_w', 'zone_c', 'hvac_w',
                 'electricity_w', 'convection_to_air_w', 'ventilation_to_air_w',
                 'hvac_to_air_w', 'longwave_to_ground_w', 'contact_to_ground_w',
                 'longwave_to_sky_w', 'storage_w', 'balance_residual_w')
OUTPUT_SURFACE_FIELDS = ('structure_into_interior_w', 'exterior_convection_to_air_w',
    'interior_convection_to_zone_w', 'interior_longwave_to_surface_w',
    'transmitted_solar_deposited_w', 'exterior_contact_to_ground_w',
    'external_net_lw_w', 'body_balance_residual_w', 'exterior_c', 'interior_c',
    'storage_w', 'storage_delta_kwh', 'incident_w', 'reflected_w', 'absorbed_w', 'transmitted_w')


def text(value):
    raw = str(value).encode('utf-8')
    return struct.pack('<I', len(raw)) + raw


def array(values):
    return struct.pack('<I', len(values)) + struct.pack(f'<{len(values)}d', *values)


def encode_models(models):
    parts = [struct.pack('<I', len(models))]
    for model in models:
        parts += [text(model['zone_id']), struct.pack('<6d', *(model[k] for k in MODEL_FIELDS)),
                  struct.pack('<I', len(model['surfaces']))]
        for surface in model['surfaces']:
            parts += [text(surface['name']), text(surface['source_element_id']), text(surface['kind']),
                      struct.pack('<I', BOUNDARIES[surface['boundary_condition']]),
                      struct.pack('<13d', *(surface[k] for k in SURFACE_FIELDS))]
            layers = surface.get('layers', ())
            parts.append(struct.pack('<I', len(layers)))
            parts.extend(struct.pack('<2d', layer['resistance_m2k_w'], layer['capacity_j_m2k'])
                         for layer in layers)
    return b''.join(parts)


def encode_step(hours, states, step_id):
    if len(hours) != len(states):
        raise ValueError('State/model count differs')
    parts = [struct.pack('<QI', step_id, len(hours))]
    for hour, state in zip(hours, states):
        parts += [array(state), struct.pack('<9d', *(hour.get(k, 0.) for k in HOUR_FIELDS))]
        parts.extend(array(hour.get(k, ())) for k in HOUR_ARRAYS)
        parts.append(struct.pack('<I', int(hour.get('hvac_enabled', True))))
    return b''.join(parts)


class Reader:
    def __init__(self, data):
        self.data = memoryview(data)
        self.position = 0

    def read(self, fmt):
        parser = struct.Struct('<' + fmt)
        if self.position + parser.size > len(self.data):
            raise ValueError('Truncated CES3 response')
        values = parser.unpack_from(self.data, self.position)
        self.position += parser.size
        return values[0] if len(values) == 1 else values

    def vector(self):
        n = self.read('I')
        if n > (len(self.data) - self.position) // 8:
            raise ValueError('Invalid CES3 vector length')
        if not n:
            return ()
        value = self.read(f'{n}d')
        return (value,) if n == 1 else value


def decode_result(raw, models, full=False):
    reader = Reader(raw)
    candidate = reader.read('Q')
    if reader.read('I') != len(models):
        raise ValueError('CES3 response model count differs')
    air, ground, residual = reader.read('3d')
    rows, states = [], []
    for model in models:
        row = dict(zip(OUTPUT_FIELDS, reader.read('13d')))
        if full:
            if reader.read('I') != len(model['surfaces']):
                raise ValueError('CES3 response surface count differs')
            row['surfaces'] = tuple(dict(name=s['name'], **dict(zip(OUTPUT_SURFACE_FIELDS, reader.read('16d'))))
                                    for s in model['surfaces'])
        rows.append(row)
        states.append(reader.vector())
    if reader.position != len(reader.data):
        raise ValueError('Trailing CES3 response bytes')
    return dict(candidate_id=candidate, air_w=air, ground_w=ground,
                max_abs_residual_w=residual, zones=tuple(rows), states=tuple(states))


class BinarySurfaceEngineV2:
    """Sequential persistent session with bounded reads and explicit state ownership."""
    def __init__(self, executable_path, timeout=120., open_process=None):
        self.timeout = timeout
        self.command = (str(executable_path), '--binary')
        self.process = (open_process or subprocess.Popen)(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0)
        self.responses = queue.Queue()
        self.errors = bytearray()
        self.request_id = 0
        self.step_id = 0
        self.models = ()
        self.active = False
        self.closed = False
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.error_reader = threading.Thread(target=self._read_errors, daemon=True)
        self.reader.start()
        self.error_reader.start()
        try:
            if struct.unpack('<3I', self.send(0, b'')) != (2, 1, 7):
                raise RuntimeError('Engine lacks CES3 v2 adiabatic boundary support')
        except BaseException:
            self.close()
            raise

    def _exact(self, n):
        parts = []
        while n:
            part = self.process.stdout.read(n)
            if not part:
                raise EOFError('CES3 v2 response stream closed')
            parts.append(part)
            n -= len(part)
        return b''.join(parts)

    def _read(self):
        try:
            while True:
                magic, version, op, request_id, length = HEADER.unpack(self._exact(HEADER.size))
                if magic != b'CES3' or version != 2 or length > MAX_FRAME:
                    raise ValueError('Invalid CES3 v2 response frame')
                self.responses.put((op, request_id, self._exact(length)))
        except Exception as error:
            self.responses.put(error)

    def _read_errors(self):
        while True:
            part = self.process.stderr.read(4096)
            if not part:
                return
            self.errors.extend(part)
            if len(self.errors) > (1 << 20):
                del self.errors[:len(self.errors) - (1 << 20)]

    def send(self, op, payload):
        if len(payload) > MAX_FRAME:
            raise ValueError('CES3 frame exceeds 256 MiB; split the zone batch')
        self.request_id += 1
        rid = self.request_id
        data = HEADER.pack(b'CES3', 2, op, rid, len(payload)) + payload
        try:
            view = memoryview(data)
            while view:
                count = self.process.stdin.write(view)
                if not count:
                    raise BrokenPipeError('CES3 short write')
                view = view[count:]
            answer = self.responses.get(timeout=self.timeout)
            if isinstance(answer, Exception):
                raise answer
            code, reply, raw = answer
            if reply != rid:
                raise RuntimeError('CES3 response request ID differs')
            if code == 65535:
                raise ValueError(raw.decode('utf-8'))
            if code != op:
                raise RuntimeError('CES3 response operation differs')
            return raw
        except (BrokenPipeError, EOFError, queue.Empty) as error:
            self.close()
            raise RuntimeError('CES3 v2 engine stopped or timed out: ' + self.stderr) from error

    @property
    def stderr(self):
        return self.errors.decode('utf-8', errors='replace')

    def configure(self, models):
        if self.active:
            raise ValueError('End the current step before configuring models')
        if not models or len({m['zone_id'] for m in models}) != len(models):
            raise ValueError('Empty or duplicate zone IDs')
        count = struct.unpack('<I', self.send(1, encode_models(models)))[0]
        if count != len(models):
            raise ValueError('CES3 registration model count differs')
        self.models = tuple(models)
        self.send(8, struct.pack('<II', 1, 1))

    def begin_step(self, hours, states):
        if self.active or len(hours) != len(self.models):
            raise ValueError('Invalid active step or model count')
        self.step_id += 1
        self.send(2, encode_step(hours, states, self.step_id))
        self.active = True

    def evaluate(self, *, full=False):
        if not self.active:
            raise ValueError('begin_step is required')
        raw = self.send(7, struct.pack('<QIIII', self.step_id, 2 if full else 3, 1, 0, 0))
        return decode_result(raw, self.models, full)

    def end_step(self):
        if self.active:
            self.send(4, struct.pack('<Q', self.step_id))
            self.active = False

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.reader.join(timeout=3)
        self.error_reader.join(timeout=3)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if not stream.closed:
                stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# Simulation orchestration and result views.

PARTS = ('cooling','heating','lighting','equipment')
MONTH_NAMES = ('Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec')
MONTH_START = np.array((0,31,59,90,120,151,181,212,243,273,304,334,365))*24


def _row(values):
    result = dict(zip(PARTS, map(float, values)))
    result['total'] = sum(result.values())
    return result


@dataclass(frozen=True)
class EnergyZoneResult:
    zone_id: str
    name: str
    area_m2: float
    energy_kwh: tuple[float, ...]

    @property
    def load(self):
        return _row(np.asarray(self.energy_kwh)/self.area_m2)

    @property
    def params(self):
        return dict(zone_name=self.name, zone_area=self.area_m2)


@dataclass(frozen=True)
class EnergyResult(SimulationResult):
    request: EnergyRequest | None = None
    zone_results: tuple[EnergyZoneResult, ...] = ()
    hour_of_year: tuple[int, ...] = ()
    zone_hourly_energy_kwh: np.ndarray | None = None
    zone_temperature_c: np.ndarray | None = None
    final_state: EnergyState | None = None
    max_balance_residual_w: float = 0.
    warmup: tuple[float, ...] = ()
    surface_hours: tuple = ()
    temporal_scale: str = 'monthly'
    spatial_scale: str = 'building'

    @property
    def data(self):
        """Derived unrounded kWh/m² view for existing analysis consumers."""
        areas = np.array([z.area_m2 for z in self.zone_results])
        absolute = self.zone_hourly_energy_kwh
        intensity = absolute / areas[:,None,None]
        building = absolute.sum(axis=0) / areas.sum()
        data = dict(total=_row(building.sum(axis=0)),spaces=self.zone_results)
        hoy = np.asarray(self.hour_of_year)
        if self.temporal_scale == 'monthly':
            months = [building[(hoy>=a)&(hoy<b)].sum(axis=0) for a,b in zip(MONTH_START[:-1],MONTH_START[1:])]
            data['months'] = dict(zip(MONTH_NAMES,map(_row,months)))
            if self.spatial_scale=='zone':
                data['zone_months'] = tuple(tuple(_row(z[(hoy>=a)&(hoy<b)].sum(axis=0)) for a,b in zip(MONTH_START[:-1],MONTH_START[1:])) for z in intensity)
        elif self.temporal_scale=='daily':
            days = tuple(np.unique(hoy//24))
            data['days'] = tuple(_row(building[hoy//24==d].sum(axis=0)) for d in days)
            if self.spatial_scale=='zone':
                data['zone_days'] = tuple(tuple(_row(z[hoy//24==d].sum(axis=0)) for d in days) for z in intensity)
        else:
            data['hours'] = tuple(map(_row,building))
            if self.spatial_scale=='zone':
                data['zone_hours'] = tuple(tuple(map(_row,z)) for z in intensity)
        return data


class EnergyRunner:
    def __init__(self, model=None, weather=None, *, energy_input=None,
                 executable_path=None, engine=None, timeout_seconds=120.,
                 temporal_scale='monthly', spatial_scale='building',
                 preheat=None, max_preheat=120, warmup_tolerance_k=.1,
                 initial_state=None, surface_results=False, on_progress=None, on_hour=None, **input_options):
        if temporal_scale not in {'monthly','daily','hourly'}:
            raise ValueError('temporal_scale must be monthly, daily, or hourly')
        if spatial_scale not in {'building','zone'}:
            raise ValueError('spatial_scale must be building or zone')
        if energy_input is not None and not isinstance(energy_input,EnergyRequest):
            raise TypeError('energy_input must be EnergyRequest; CSV/CLI zone dictionaries are no longer supported')
        self.request = energy_input if energy_input is not None else build_energy_input(model,weather,**input_options)
        suffix = '.exe' if platform.system()=='Windows' else ''
        packaged = Path(__file__).resolve().parents[2]/'libs'/'energy'/f'CetusSurfaceEnergy{suffix}'
        self.executable = Path(executable_path or os.environ.get('MOOSAS_ENERGY_EXECUTABLE',packaged)).resolve()
        if not self.executable.is_file():
            raise FileNotFoundError(f'CETUS CES3 v2 executable missing: {self.executable}')
        if os.name!='nt' and not os.access(self.executable,os.X_OK):
            self.executable.chmod(self.executable.stat().st_mode | 0o111)
        self.engine = engine or SubprocessEngine()
        if not np.isfinite(timeout_seconds) or timeout_seconds<=0:
            raise ValueError('timeout_seconds must be positive')
        self.timeout_seconds = timeout_seconds
        self.temporal_scale = temporal_scale
        self.spatial_scale = spatial_scale
        self.preheat = (0 if initial_state is not None else 4) if preheat is None else preheat
        if int(self.preheat)!=self.preheat or self.preheat<0 or int(max_preheat)!=max_preheat or max_preheat<self.preheat or warmup_tolerance_k<=0:
            raise ValueError('Invalid warmup controls')
        if self.preheat and (initial_state is not None or self.request.start_hour+24>8760):
            raise ValueError('Restart/end-of-year windows require preheat=0')
        self.max_preheat = max_preheat
        self.tolerance = warmup_tolerance_k
        self.initial_state = initial_state
        self.surface_results = surface_results
        self.on_progress = on_progress
        self.on_hour = on_hour

    def run(self):
        request = self.request
        signature = request.signature
        zone_ids = tuple(z.zone_id for z in request.zones)
        if self.initial_state is not None:
            state = self.initial_state
            if state.model_signature!=signature or state.zone_ids!=zone_ids or state.next_hour!=request.start_hour:
                raise ValueError('Restart signature, zone ordering, or next hour differs from request')
            states = state.states
        else:
            states = tuple(() for _ in request.zones)
        models = tuple(z.native_model() for z in request.zones)
        energy = np.zeros((len(models),request.hours,4))
        temperatures = np.zeros((len(models),request.hours))
        maximum_residual = 0.
        history, surfaces = [], []
        warnings = list(request.warnings)
        session = BinarySurfaceEngineV2(self.executable,self.timeout_seconds,self.engine.open_binary_session)
        with session:
            session.configure(models)
            def step(hour, full=False):
                nonlocal states,maximum_residual
                session.begin_step(request.hour(hour),states)
                response = session.evaluate(full=full)
                states = response['states']
                maximum_residual = max(maximum_residual,response['max_abs_residual_w'])
                session.end_step()
                return response['zones']
            if self.preheat:
                for cycle in range(self.max_preheat):
                    before = states
                    for hour in range(24):
                        step(hour)
                    change = max((max(abs(a-b) for a,b in zip(old,new)) if old else float('inf')) for old,new in zip(before,states))
                    history.append(change)
                    if cycle+1>=self.preheat and change<=self.tolerance:
                        break
                else:
                    warnings.append(f'Warmup did not converge to {self.tolerance} K in {self.max_preheat} daily cycles')
            for hour in range(request.hours):
                rows = step(hour,self.surface_results or self.on_hour is not None)
                for i,(zone,row) in enumerate(zip(request.zones,rows)):
                    q = row['hvac_w']
                    h = request.start_hour+hour
                    energy[i,hour] = (max(-q,0)/zone.cooling_cop/1000,
                                      max(q,0)/zone.heating_cop/1000,
                                      zone.lighting_w[h]/1000,zone.equipment_w[h]/1000)
                    temperatures[i,hour] = row['zone_c']
                if self.surface_results:
                    surfaces.append(tuple(row['surfaces'] for row in rows))
                if self.on_hour is not None:
                    self.on_hour(request.start_hour+hour,rows)
                if self.on_progress and ((hour+1)%24==0 or hour+1==request.hours):
                    self.on_progress(hour+1,request.hours)
        energy.setflags(write=False)
        temperatures.setflags(write=False)
        results = tuple(EnergyZoneResult(z.zone_id,z.name,z.area_m2,tuple(energy[i].sum(axis=0))) for i,z in enumerate(request.zones))
        checkpoint = EnergyState(signature,request.start_hour+request.hours,zone_ids,states)
        command = CommandResult(session.command,session.process.returncode,'CES3 v2 numeric session',session.stderr)
        return EnergyResult(commands=(command,),warnings=tuple(warnings),request=request,zone_results=results,
            hour_of_year=tuple(range(request.start_hour,request.start_hour+request.hours)),zone_hourly_energy_kwh=energy,
            zone_temperature_c=temperatures,final_state=checkpoint,max_balance_residual_w=maximum_residual,
            warmup=tuple(history),surface_hours=tuple(surfaces),temporal_scale=self.temporal_scale,spatial_scale=self.spatial_scale)
