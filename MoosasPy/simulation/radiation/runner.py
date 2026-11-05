"""Isolated execution support for Radiance daylight calculations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import os
import shutil
import csv
import json
import hashlib
import shlex
import numpy as np
from typing import TYPE_CHECKING, TextIO

from ...utils import path
from ...utils.tools import native_platform
from ..contracts import Location, SimulationResult
from ..engine import NativeEngine, SubprocessEngine
from ..runner import CommandError, CommandResult, CommandTimeoutError, Runner
from ..workspace import SimulationWorkspace
from .scene import _write_grid, _daylight_scene

if TYPE_CHECKING:
    from ...model import MoosasModel
    from ...transform.geometry.element import MoosasElement


@dataclass(frozen=True)
class RadianceSky:
    """Parameters used to generate a Radiance sky description."""

    date: datetime
    sky_type: str
    location: Location
    diffuse_illuminance: float = 15000.0
    utc_offset_hours: float = 0.0
    direct_normal_irradiance_w_m2: float | None = None
    diffuse_horizontal_irradiance_w_m2: float | None = None


RadianceCommandResult = CommandResult


@dataclass(frozen=True)
class DaylightFloorResult:
    """Daylight metrics for one model floor element."""

    uid: str
    element: MoosasElement
    grid_point_count: int
    illuminances: tuple[float, ...]
    daylight_factor: float | None
    satisfied_fraction: float


@dataclass(frozen=True)
class RadianceDaylightResult(SimulationResult):
    """Structured output from an isolated Radiance daylight run."""

    floors: tuple[DaylightFloorResult, ...] = ()


class RadianceCommandError(CommandError):
    """Raised when a Radiance executable exits with a non-zero status."""

class RadianceTimeoutError(CommandTimeoutError):
    """Raised when a Radiance executable exceeds the configured timeout."""

class RadianceRunner(Runner):
    """Run a daylight calculation in an isolated temporary work directory."""

    result_type = RadianceCommandResult
    error_type = RadianceCommandError
    timeout_error_type = RadianceTimeoutError

    _RTRACE_ARGUMENTS = (
        "-w", "-h", "-I+", "-u", "-aa", "0.15", "-ab", "4", "-ad", "256",
        "-ar", "32", "-as", "20", "-st", "1", "-lw", "0.05", "-dc", "0",
        "-dj", "0.7", "-dp", "32", "-dr", "0", "-ds", "0",
    )

    def __init__(
        self,
        model: MoosasModel,
        sky: RadianceSky | None = None,
        work_dir: str | Path | None = None,
        timeout_seconds: float = 300.0,
        executable_dir: str | Path | None = None,
        engine: NativeEngine | None = None,
    ):
        super().__init__(timeout_seconds=timeout_seconds, engine=engine)
        self._input_commands = ()
        self.model = model
        self.sky = sky
        self.work_dir = Path(work_dir) if work_dir is not None else None
        if executable_dir is not None:
            self.executable_dir = Path(executable_dir).resolve()
        else:
            platform_dir, _ = native_platform()
            self.executable_dir = Path(path.libDir) / 'radiance' / platform_dir / 'bin'
        if engine is None:
            env = dict(os.environ)
            env['PATH'] = str(self.executable_dir) + os.pathsep + env.get('PATH', '')
            env['RAYPATH'] = os.pathsep.join(['.', str(self.executable_dir.parent / 'lib')])
            self.engine = SubprocessEngine(env=env)

    def run(self) -> RadianceDaylightResult:
        """Generate inputs, run Radiance, and return parsed daylight metrics."""
        if self.sky is None:
            raise ValueError('PIT requires RadianceSky; annual calls use run_annual(request)')
        with SimulationWorkspace(parent=self.work_dir, prefix="moosas-radiance-", retain=self.work_dir is not None) as workspace:
            run_dir = workspace.path
            rad_path = run_dir / "model.rad"
            grid_path = run_dir / "grid.input"
            octree_path = run_dir / "model.oct"
            output_path = run_dir / "illuminance.output"

            floors = self._write_inputs(rad_path, grid_path)
            if not floors:
                return RadianceDaylightResult(floors=(), commands=(), workspace=workspace.report)

            oconv_result = self._compile_scene(rad_path, octree_path, run_dir)
            rtrace_result, illuminances = self._trace_illuminance(octree_path, grid_path, output_path, run_dir)
            floor_results = self._summarize_floors(floors, illuminances)
            return RadianceDaylightResult(
                floors=tuple(floor_results),
                commands=(*self._input_commands, oconv_result, rtrace_result),
                workspace=workspace.report,
            )

    def _write_inputs(self, rad_path: Path, grid_path: Path) -> list[tuple[MoosasElement, int]]:
        from .radiance import _get_sky
        self._input_commands = ()
        dni = self.sky.direct_normal_irradiance_w_m2
        dhi = self.sky.diffuse_horizontal_irradiance_w_m2
        if dni is not None or dhi is not None:
            if dni is None or dhi is None or not np.isfinite([dni, dhi]).all() or min(dni, dhi) < 0:
                raise ValueError('Weather PIT requires finite nonnegative DNI and DHI together')
            date = self.sky.date
            offset = self.sky.utc_offset_hours if date.utcoffset() is None else date.utcoffset().total_seconds() / 3600
            hour = date.hour + date.minute / 60 + date.second / 3600
            command = (str(self._executable('gendaylit')), str(date.month), str(date.day), str(hour),
                '-a', str(self.sky.location.latitude), '-o', str(-self.sky.location.longitude),
                '-m', str(-15 * offset), '-W', str(dni), str(dhi), '-O', '0')
            result = self._run_command(command, rad_path.parent)
            self._input_commands = (result,)
            hemisphere = _get_sky(date, '-c', 0, 0).split('\n', 1)[1]
            # Ground must be neutral to match gendaymtx's default ground color.
            hemisphere = hemisphere.replace('1 .8 .5 0', '1 1 1 0')
        else:
            command_text, hemisphere = _get_sky(
                self.sky.date, self.sky.sky_type,
                self.sky.location.latitude, self.sky.location.longitude,
                self.sky.diffuse_illuminance, self.sky.utc_offset_hours,
            ).split('\n', 1)
            arguments = shlex.split(command_text[1:])
            command = (str(self._executable(arguments[0])), *arguments[1:])
            result = self._run_command(command, rad_path.parent)
            self._input_commands = (result,)
        if not result.stdout.strip():
            raise ValueError('Radiance sky generator returned empty output')
        rad_path.write_text(result.stdout + '\n' + hemisphere + '\n' + _daylight_scene(self.model)[0], encoding='utf-8')
        floors = []
        for space in self.model.spaceList:
            for floor in space.getAllFaces(to_dict=True)["MoosasFloor"]:
                if floor.category==2:continue
                grid_lines = _write_grid(
                    floor,
                        grid_path=str(grid_path),
                    normal=[0, 0, 1],
                    append=bool(floors),
                )
                floors.append((floor, len(grid_lines)))
        return floors

    def _compile_scene(self, rad_path: Path, octree_path: Path, run_dir: Path) -> RadianceCommandResult:
        command = (str(self._executable("oconv")), rad_path.name)
        with octree_path.open("wb") as octree_file:
            return self._run_command(command, run_dir, stdout=octree_file)

    def _trace_illuminance(
        self,
        octree_path: Path,
        grid_path: Path,
        output_path: Path,
        run_dir: Path,
    ) -> tuple[RadianceCommandResult, tuple[float, ...]]:
        command = (str(self._executable("rtrace")), *self._RTRACE_ARGUMENTS, octree_path.name)
        with grid_path.open("r", encoding="utf-8") as grid_file:
            command_result = self._run_command(command, run_dir, stdin=grid_file)

        illuminances = self._parse_illuminances(command_result.stdout)
        output_path.write_text("\n".join(str(value) for value in illuminances) + "\n", encoding="utf-8")
        return command_result, illuminances

    def _summarize_floors(
        self,
        floors: list[tuple[MoosasElement, int]],
        illuminances: tuple[float, ...],
    ) -> list[DaylightFloorResult]:
        expected_count = sum(grid_count for _, grid_count in floors)
        if len(illuminances) != expected_count:
            raise ValueError(f"Radiance returned {len(illuminances)} values for {expected_count} grid points.")

        floor_results = []
        offset = 0
        for floor, grid_count in floors:
            values = illuminances[offset:offset + grid_count]
            offset += grid_count
            average = sum(values) / grid_count
            floor_results.append(
                DaylightFloorResult(
                    uid=floor.Uid,
                    element=floor,
                    grid_point_count=grid_count,
                    illuminances=values,
                    daylight_factor=(average / self.sky.diffuse_illuminance if self.sky.direct_normal_irradiance_w_m2 is None else None),
                    satisfied_fraction=sum(value > 300.0 for value in values) / grid_count,
                )
            )
        return floor_results

    def _run_command(
        self,
        command: tuple[str, ...],
        run_dir: Path,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
    ) -> RadianceCommandResult:
        return self.run_command(command, cwd=run_dir, stdin=stdin, stdout=stdout)

    def _executable(self, name: str) -> Path:
        executable_name = f"{name}.exe" if os.name == "nt" else name
        executable = self.executable_dir / executable_name
        if not executable.is_file():
            raise RadianceCommandError((str(executable),), -1, '', 'Radiance executable was not found in executable_dir.')
        return executable

    def run_annual(self, request: AnnualDaylightRequest) -> AnnualDaylightResult:
        """Compute fixed-state annual daylight using two-phase DDS correction."""
        return _run_annual(self, request)

    @staticmethod
    def _parse_illuminances(output: str) -> tuple[float, ...]:
        values = []
        for line_number, line in enumerate(output.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                red, green, blue = (float(value) for value in line.split()[:3])
            except ValueError as error:
                raise ValueError(f"Invalid rtrace output on line {line_number}: {line!r}") from error
            values.append((red * 0.265 + green * 0.670 + blue * 0.065) * 179)
        return tuple(values)

    _decode_output = staticmethod(Runner._decode_output)


@dataclass(frozen=True)
class AnnualDaylightRequest:
    """Prepared WEA plus numerical options; geometry/settings stay in MoosasModel.

    Masks are 8760 boolean entries keyed by space ID, with no hidden calendar.
    None uses each space's daylight_occupied_mask or work_start/work_end daily.
    North rotation is counter-clockwise, west of the model's positive Y axis.
    """
    wea_file: str
    output_dir: str | None = None
    grid_spacing_m: float = .5
    workplane_height_m: float = .76
    sky_subdivision: int = 2
    north_rotation_deg: float = 0.
    occupied_by_space: dict | None = None
    optical_overrides: dict | None = None
    space_ids: tuple[str, ...] | None = None
    ambient_bounces: int = 5
    ambient_divisions: int = 4096
    processes: int = 1
    da_threshold_lux: float = 300.
    required_time_fraction: float = .5
    udi_lower_lux: float = 100.
    udi_upper_lux: float = 2000.
    ase_threshold_lux: float = 1000.
    ase_hours: float = 250.

    def __post_init__(self):
        for key in ('grid_spacing_m', 'workplane_height_m', 'da_threshold_lux', 'ase_threshold_lux'):
            value = getattr(self, key)
            if not np.isfinite(value) or value <= 0:
                raise ValueError(f'{key} must be finite and positive')
        for key in ('sky_subdivision', 'ambient_divisions', 'ambient_bounces', 'processes'):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f'{key} must be a positive integer')
        if not np.isfinite(self.north_rotation_deg) or not 0 < self.required_time_fraction <= 1:
            raise ValueError('Invalid north rotation or required_time_fraction')
        if not 0 <= self.udi_lower_lux < self.udi_upper_lux < np.inf or not 0 <= self.ase_hours < np.inf:
            raise ValueError('Invalid UDI bounds or ASE duration')


@dataclass(frozen=True)
class AnnualDaylightResult(SimulationResult):
    """File-backed annual results; all paths outlive the runner call."""
    manifest_file: str = ''
    illuminance_total_file: str = ''
    illuminance_direct_file: str = ''
    sensor_count: int = 0
    time_count: int = 8760


def _read_daylight_wea(file):
    from datetime import timedelta
    lines = Path(file).read_text(encoding='utf-8-sig').splitlines()
    expected = ('place', 'latitude', 'longitude', 'time_zone', 'site_elevation', 'weather_data_file_units')
    if len(lines) != 8766 or any(not lines[i].startswith(key + ' ') for i, key in enumerate(expected)):
        raise ValueError('Annual daylight needs a complete hourly non-leap WEA (8760 records)')
    latitude, longitude, meridian, altitude, units = [float(lines[i].split()[-1]) for i in range(1, 6)]
    if not np.isfinite([latitude, longitude, meridian, altitude]).all() or not (-90 <= latitude <= 90 and -180 <= longitude <= 180 and -210 <= meridian <= 210) or units != 1:
        raise ValueError('Invalid WEA location or units')
    values = np.loadtxt(lines[6:])
    if values.shape != (8760, 5) or not np.isfinite(values).all() or np.any((values[:, 3:] < 0) | (values[:, 3:] >= 9999)):
        raise ValueError('WEA contains invalid/missing DNI/DHI')
    stamps = [datetime(2001, 1, 1) + timedelta(hours=i, minutes=30) for i in range(8760)]
    axis = np.array([(d.month, d.day, d.hour + .5) for d in stamps])
    if not np.allclose(values[:, :3], axis, rtol=0, atol=1e-8):
        raise ValueError('WEA must use ordered hourly local-standard-time midpoints')
    return values, stamps, dict(latitude=latitude, longitude_east=-longitude, utc_offset_hours=-meridian / 15, altitude_m=altitude)


def _daylight_sensors(model, request):
    """Clip XY cells to complete space floors, preserving holes and pieces."""
    import shapely
    sensors, spaces = [], []
    selected = set(request.space_ids) if request.space_ids is not None else {str(s.id) for s in model.spaceList}
    unknown = selected - {str(s.id) for s in model.spaceList}
    if unknown:
        raise ValueError(f'Unknown daylight spaces: {sorted(unknown)}')
    for space in sorted(model.spaceList, key=lambda s: str(s.id)):
        if str(space.id) not in selected:
            continue
        if hasattr(space, 'height') and request.workplane_height_m >= float(space.height):
            raise ValueError(f'{space.id}: workplane must be below the ceiling')
        floors = sorted(space.getAllFaces(to_dict=True)['MoosasFloor'], key=lambda f: str(f.Uid))
        if not floors:
            raise ValueError(f'{space.id}: no analysis floor')
        parts = [shapely.force_2d(f.face) for f in floors]
        region = shapely.union_all(parts)
        if not region.is_valid or region.area <= 0:
            raise ValueError(f'{space.id}: invalid analysis floor')
        # Voids stay excluded even when the parent floor lacks a punched hole.
        for void in getattr(space, 'void', ()):
            region = shapely.difference(region, shapely.force_2d(void.force_2d()))
        spacing = request.grid_spacing_m
        xmin, ymin, xmax, ymax = region.bounds
        area = 0.
        for ix in range(int(np.floor(xmin / spacing)), int(np.ceil(xmax / spacing))):
            for iy in range(int(np.floor(ymin / spacing)), int(np.ceil(ymax / spacing))):
                clipped = shapely.intersection(region, shapely.box(ix * spacing, iy * spacing, (ix+1) * spacing, (iy+1) * spacing))
                for piece in shapely.get_parts(clipped):
                    if piece.area <= 1e-10:
                        continue
                    point = piece.centroid
                    if not piece.contains(point):
                        point = piece.representative_point()
                    floor = floors[max(range(len(parts)), key=lambda i: shapely.intersection(parts[i], piece).area)]
                    coordinates = shapely.get_coordinates(floor.face, include_z=True)
                    if np.ptp(coordinates[:, 2]) > 1e-4:
                        raise ValueError(f'{floor.Uid}: sloped workplanes are not supported')
                    z = float(coordinates[0, 2]) + request.workplane_height_m
                    sensors.append(dict(sensor_id=f'{space.id}:{len(sensors)}', space_id=str(space.id), floor_uid=str(floor.Uid),
                                        x=point.x, y=point.y, z=z, nx=0, ny=0, nz=1, represented_area_m2=piece.area))
                    area += piece.area
        if not np.isclose(area, region.area, rtol=1e-8, atol=1e-7):
            raise ValueError(f'{space.id}: daylight grid area does not close')
        if area <= 0:
            raise ValueError(f'{space.id}: empty analysis area')
        spaces.append(space)
    if not sensors:
        raise ValueError('No daylight sensors')
    return sensors, spaces


def _read_radiance_matrix(file, rows, columns):
    """Read checked RGB matrices; Radiance defaults to ASCII on every platform."""
    with open(file, 'rb') as source:
        header = {}
        while True:
            line = source.readline()
            if not line:
                raise ValueError(f'Missing Radiance matrix header: {file}')
            if not line.strip():
                break
            text = line.decode('ascii').strip()
            if '=' in text:
                key, value = text.split('=', 1)
                header[key] = value
        dimensions = ('NROWS', 'NCOLS', 'NCOMP')
        if not all(key in header for key in dimensions):
            raise ValueError(f'Missing Radiance matrix dimensions: {file}, {header}')
        if tuple(int(header[key]) for key in dimensions) != (rows, columns, 3):
            raise ValueError(f'Unexpected Radiance matrix dimensions: {file}, {header}')
        if header.get('FORMAT') == 'ascii':
            data = np.fromstring(source.read().decode('ascii'), sep=' ', dtype=np.float32)
        elif header.get('FORMAT') in ('float', 'double'):
            data = np.memmap(file, mode='r', offset=source.tell(), dtype=np.float32 if header['FORMAT'] == 'float' else np.float64)
        else:
            raise ValueError(f'Unsupported Radiance matrix format: {header}')
    if data.size != rows * columns * 3 or not np.isfinite(data).all():
        raise ValueError(f'Invalid/incomplete Radiance RGB matrix: {file}')
    return data.reshape(rows, columns, 3)


def _write_csv(file, records):
    if not records:
        return
    with open(file, 'w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)


def _daylight_metrics(total, direct, sensors, spaces, request):
    points, summaries = [], []
    masks = {}
    supplied = request.occupied_by_space or {}
    if set(supplied) - {str(s.id) for s in spaces}:
        raise ValueError('Occupancy overrides reference unevaluated spaces')
    for space in spaces:
        uid = str(space.id)
        mask = supplied.get(uid, space.settings.get('daylight_occupied_mask'))
        if mask is None:
            start = float(space.settings.get('zone_work_start', 8))
            end = float(space.settings.get('zone_work_end', 18))
            if not 0 <= start <= 24 or not 0 <= end <= 24:
                raise ValueError(f'{uid}: invalid daily daylight hours')
            hours = np.arange(8760) % 24 + .5
            mask = (hours >= start) & (hours < end) if start <= end else (hours >= start) | (hours < end)
        mask = np.asarray(mask)
        if mask.shape != (8760,) or not np.isin(mask, [False, True]).all():
            raise ValueError(f'{uid}: occupancy requires 8760 boolean entries')
        mask = mask.astype(bool)
        masks[uid] = mask
        indices = [i for i, sensor in enumerate(sensors) if sensor['space_id'] == uid]
        occupied = int(mask.sum())
        for i in indices:
            values = total[i, mask]
            beam = direct[i, mask]
            points.append(dict(sensor_id=sensors[i]['sensor_id'], space_id=uid,
                da=float(np.mean(values >= request.da_threshold_lux)) if occupied else None,
                cda=float(np.mean(np.minimum(values / request.da_threshold_lux, 1))) if occupied else None,
                udi_low=float(np.mean(values < request.udi_lower_lux)) if occupied else None,
                udi_useful=float(np.mean((values >= request.udi_lower_lux) & (values <= request.udi_upper_lux))) if occupied else None,
                udi_high=float(np.mean(values > request.udi_upper_lux)) if occupied else None,
                direct_exceedance_hours=int(np.sum(beam > request.ase_threshold_lux)) if occupied else None))
        area = sum(sensors[i]['represented_area_m2'] for i in indices)
        point_map = {p['sensor_id']: p for p in points if p['space_id'] == uid}
        sda = sum(sensors[i]['represented_area_m2'] for i in indices if occupied and point_map[sensors[i]['sensor_id']]['da'] >= request.required_time_fraction) / area
        ase = sum(sensors[i]['represented_area_m2'] for i in indices if occupied and point_map[sensors[i]['sensor_id']]['direct_exceedance_hours'] > request.ase_hours) / area
        summaries.append(dict(space_id=uid, analysis_area_m2=area, occupied_hours=occupied,
                              sda=sda if occupied else None, ase=ase if occupied else None,
                              status='ok' if occupied else 'no_occupied_hours'))
    return points, summaries, masks


def _run_annual(runner, request):
    weather, stamps, location = _read_daylight_wea(request.wea_file)
    # Resolve all required native tools before doing expensive preparation.
    executables = {name: runner._executable(name) for name in ('oconv', 'rfluxmtx', 'rcontrib', 'gendaymtx')}
    sensors, spaces = _daylight_sensors(runner.model, request)
    normal_scene, optics = _daylight_scene(runner.model, overrides=request.optical_overrides)
    black_scene, _ = _daylight_scene(runner.model, black=True, overrides=request.optical_overrides)
    coefficient_scene, _ = _daylight_scene(runner.model, overrides=request.optical_overrides, split_transport=True)
    with SimulationWorkspace(parent=request.output_dir or runner.work_dir, prefix='moosas-annual-', retain=True) as workspace:
        root = workspace.path
        commands = []
        def execute(name, arguments, output, stdin=None):
            with (root / output).open('wb') as stream:
                if stdin:
                    with (root / stdin).open('r', encoding='utf-8') as source:
                        result = runner._run_command((str(executables[name]), *map(str, arguments)), root, stdin=source, stdout=stream)
                else:
                    result = runner._run_command((str(executables[name]), *map(str, arguments)), root, stdout=stream)
            commands.append(result)
        (root / 'scene.rad').write_text(normal_scene, encoding='utf-8')
        (root / 'black.rad').write_text(black_scene, encoding='utf-8')
        (root / 'coefficients.rad').write_text(coefficient_scene, encoding='utf-8')
        shutil.copyfile(request.wea_file, root / 'weather.wea')
        (root / 'sensors.pts').write_text(''.join(f"{s['x']} {s['y']} {s['z']} 0 0 1\n" for s in sensors), encoding='utf-8')
        mf = request.sky_subdivision
        patches = 144 * mf * mf + 2
        (root / 'receiver.rad').write_text(
            '#@rfluxmtx h=u\nvoid glow ground_glow\n0\n0\n4 1 1 1 0\nground_glow source ground\n0\n0\n4 0 0 -1 180\n'
            f'#@rfluxmtx h=r{mf} u=Y\nvoid glow sky_glow\n0\n0\n4 1 1 1 0\nsky_glow source sky\n0\n0\n4 0 0 1 180\n', encoding='utf-8')
        sky_args = ['-O0', '-m', mf, '-r', request.north_rotation_deg]
        # ASCII avoids binary stdio incompatibilities in Windows Radiance builds.
        execute('gendaymtx', [*sky_args, '-D', 'suns.rad', '-M', 'sunmods.txt', 'weather.wea'], 'sky.mtx')
        execute('gendaymtx', [*sky_args, '-d', 'weather.wea'], 'sun-coarse.mtx')
        trace_args = ['-I+', '-y', len(sensors), '-n', request.processes, '-ad', request.ambient_divisions,
                      '-lw', 1e-5, '-aa', 0, '-faf']
        # -faf: ASCII sensor input, native float matrix output (Radiance supports this on Windows).
        # Our supported optics are spectrally neutral. Encode full transport in
        # red/blue and direct-only transport in green (opaque green=0).
        # Both coefficients share every sampled path, so direct terms cancel
        # exactly instead of subtracting two noisy independent Monte Carlo runs.
        execute('rfluxmtx', [*trace_args, '-ab', request.ambient_bounces, '-', 'receiver.rad', 'coefficients.rad'], 'coeff.mtx', 'sensors.pts')
        sunmods = (root / 'sunmods.txt').read_text().split()
        n = len(sensors)
        direct = np.lib.format.open_memmap(root / 'illuminance_direct.npy', mode='w+', dtype='float32', shape=(n, 8760))
        direct[:] = 0
        if sunmods:
            execute('oconv', ['black.rad', 'suns.rad'], 'sun.oct')
            execute('rcontrib', ['-I+', '-V+', '-ab', 0, '-n', request.processes, '-y', n, '-faf', '-dc', 1,
                                  '-dt', 0, '-dj', 0, '-st', 0, '-ss', 0, '-lw', 1e-6, '-M', 'sunmods.txt', 'sun.oct'], 'sun-exact.mtx', 'sensors.pts')
            sun_rgb = _read_radiance_matrix(root / 'sun-exact.mtx', n, len(sunmods))
            indices = np.array([(int(name.removeprefix('solar')) - 30) // 60 for name in sunmods])
            if len(set(indices)) != len(indices) or np.any(indices < 0) or np.any(indices >= 8760) or any(name != f'solar{int(i)*60+30}' for name, i in zip(sunmods, indices)):
                raise ValueError('Radiance sun modifiers do not map to the full WEA axis')
            for first in range(0, n, 128):
                direct[first:first+128, indices] = sun_rgb[first:first+128] @ np.array([.265, .670, .065], dtype=np.float32) * 179
            del sun_rgb
        sky = _read_radiance_matrix(root / 'sky.mtx', patches, 8760)
        sun = _read_radiance_matrix(root / 'sun-coarse.mtx', patches, 8760)
        coeff = _read_radiance_matrix(root / 'coeff.mtx', n, patches)
        if np.min(coeff[:, :, 0] - coeff[:, :, 1]) < -1e-5:
            raise ValueError('Encoded direct coefficient exceeds full transport coefficient')
        weights = np.array([.265, .670, .065], dtype=np.float32) * 179
        sky_lux = sky @ weights
        sun_lux = sun @ weights
        diffuse_lux = np.maximum(sky_lux - sun_lux, 0)
        del sky, sun
        total = np.lib.format.open_memmap(root / 'illuminance_total.npy', mode='w+', dtype='float32', shape=(n, 8760))
        # Achromatic optics let us combine the photopic sky before transport;
        # the red/green channels above encode paths, not physical spectral bands.
        for first in range(0, n, 128):
            last = min(first + 128, n)
            full = coeff[first:last, :, 0]
            reflected = np.maximum(full - coeff[first:last, :, 1], 0)
            total[first:last] = direct[first:last] + full @ diffuse_lux + reflected @ sun_lux
        direct.flush()
        total.flush()
        points, summaries, masks = _daylight_metrics(total, direct, sensors, spaces, request)
        _write_csv(root / 'sensors.csv', sensors)
        _write_csv(root / 'metrics_points.csv', points)
        _write_csv(root / 'metrics_spaces.csv', summaries)
        _write_csv(root / 'time_axis.csv', [dict(time_index=i, local_standard_time=t.isoformat(), duration_hours=1,
            **{f'occupied:{uid}': int(mask[i]) for uid, mask in masks.items()}) for i, t in enumerate(stamps)])
        warnings = ['Fixed optical/shading state; this is not a full LM-83 shading-operation assessment.']
        if any(record['assumed'] for record in optics):
            warnings.append('Missing visible optical properties use declared defaults; legacy transparency/reflection and SHGC are not VLT.')
        if any(str(s.id) not in (request.occupied_by_space or {}) and 'daylight_occupied_mask' not in s.settings for s in spaces):
            warnings.append('Unspecified occupancy uses zone_work_start/end every day, without weekday/holiday inference.')
        def digest(file):
            return hashlib.sha256((root / file).read_bytes()).hexdigest()
        manifest = dict(schema_version=1, method='Radiance two-phase DDS with paired achromatic transport channels', metric_profile='fixed_state', shading_state='as_modelled',
            shape=[n, 8760], dtype='float32', units='lux', axis_order=['sensor', 'time'], sky_subdivision=mf,
            sky_rows='ground first; Reinhart sky follows', location=location, time_basis='local standard, hour midpoint, non-leap synthetic year 2001',
            request={k:v for k,v in vars(request).items() if k not in ('occupied_by_space', 'optical_overrides')}, optics=optics,
            files={k:k for k in ('sensors.csv', 'time_axis.csv', 'illuminance_total.npy', 'illuminance_direct.npy', 'metrics_points.csv', 'metrics_spaces.csv')},
            hashes={k:digest(k) for k in ('scene.rad','coefficients.rad','weather.wea','sensors.pts','receiver.rad')}, warnings=warnings,
            tools={name:dict(path=str(file), sha256=hashlib.sha256(file.read_bytes()).hexdigest()) for name,file in executables.items()},
            commands=[dict(command=list(c.command), returncode=c.returncode, stderr=c.stderr) for c in commands])
        (root / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        return AnnualDaylightResult(commands=tuple(commands), warnings=tuple(warnings), workspace=workspace.report,
            manifest_file=str(root / 'manifest.json'), illuminance_total_file=str(root / 'illuminance_total.npy'),
            illuminance_direct_file=str(root / 'illuminance_direct.npy'), sensor_count=n)
