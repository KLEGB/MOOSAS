"""CES3 v2 reference transport. SI numeric fields; no JSON in the wire path.

The identical codec is vendored by MoosasPy so neither application imports the
other application's domain model. Wire compatibility is tested across copies.
"""
from __future__ import annotations

import queue
import struct
import subprocess
import threading

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
