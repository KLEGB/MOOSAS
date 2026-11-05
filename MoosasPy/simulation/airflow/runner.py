"""Model-driven CONTAM airflow simulation."""

from dataclasses import dataclass
import os
import re
import shutil

import numpy as np

from ...model import MoosasModel
from ...utils.tools import path, native_platform
from ..contracts import SimulationResult
from ..engine import NativeEngine
from ..runner import Runner
from ..workspace import SimulationWorkspace, WorkspaceReport
from .network import (
    AfnPath, AfnZone, buildNetworkFile, getZoneAndPath,
    cleanseNetwork, applyWindPressure,
)
from ...transform.geometry.geos import Vector
from .parser import (
    AIR_DENSITY,
    build_matrix,
    build_mass_matrix,
    read_file,
    read_flowpath,
    read_topology,
    write_file,
)


@dataclass(frozen=True)
class VentPaths:
    """Native CONTAM resources and isolated runtime paths for one run."""

    workspace: str
    contamx: str
    simread: str
    response: str
    contam_dir: str
    project_dir: str
    result_dir: str

    @classmethod
    def create(cls, work_dir=None):
        with SimulationWorkspace(parent=work_dir, prefix="moosas-vent-", retain=True) as workspace:
            return cls.from_workspace(workspace.path)

    @classmethod
    def from_workspace(cls, workspace):
        workspace = os.path.abspath(workspace)
        platform_dir, executable_suffix = native_platform()
        contam_root = os.path.join(path.libDir, "contam")
        bundled_dir = os.path.join(contam_root, platform_dir, "bin")
        contam_dir = os.path.join(workspace, "contam")
        project_dir = os.path.join(workspace, "project")
        result_dir = os.path.join(workspace, "result")
        os.makedirs(contam_dir, exist_ok=True)
        os.makedirs(project_dir, exist_ok=True)
        os.makedirs(result_dir, exist_ok=True)
        contamx_name = f"contamx3{executable_suffix}"
        simread_name = f"simread{executable_suffix}"
        for executable_name in (contamx_name, simread_name):
            target = os.path.join(contam_dir, executable_name)
            shutil.copy2(os.path.join(bundled_dir, executable_name), target)
            if not executable_suffix:
                os.chmod(target, 0o755)
        return cls(
            workspace=workspace,
            contamx=os.path.join(contam_dir, contamx_name),
            simread=os.path.join(contam_dir, simread_name),
            response=os.path.join(contam_root, "response.txt"),
            contam_dir=contam_dir,
            project_dir=project_dir,
            result_dir=result_dir,
        )


@dataclass(frozen=True)
class AirflowZoneResult:
    """Temperature and air-change histories for one model zone."""

    user_name: str
    project_name: str
    heat_load: float
    volume: float
    temperatures: tuple[float, ...]
    ach_values: tuple[float, ...]


@dataclass(frozen=True)
class AirflowResult(SimulationResult):
    """Structured output from one iterative CONTAM airflow calculation."""

    airflow_matrix: np.ndarray | None = None
    zones: tuple[AirflowZoneResult, ...] = ()
    converged: bool = False
    iteration_count: int = 0
    residual: float = float("inf")
    path_results: tuple[dict, ...] = ()
    mass_flow_matrix_kg_s: np.ndarray | None = None


class AirflowRunner(Runner):
    """Build and iteratively solve a CONTAM airflow model."""

    def __init__(
        self,
        model: MoosasModel,
        outdoor_temperature=25.0,
        heat_loads: dict[str, float] | None = None,
        max_iterations=50,
        convergence_tolerance=0.01,
        flow_multiplier=1.0,
        contam_exe=None,
        simread_exe=None,
        response_file=None,
        paths: VentPaths | None = None,
        work_dir=None,
        timeout_seconds=300.0,
        engine: NativeEngine | None = None,
        wind_direction_vector=None,
        wind_speed=0.0,
        indoor_temperature=None,
        thermal=True,
        alpha=0.22,
        network_dict=None,
    ):
        super().__init__(timeout_seconds=timeout_seconds, engine=engine)
        if int(max_iterations) <= 0:
            raise ValueError("max_iterations must be positive.")
        if float(convergence_tolerance) < 0:
            raise ValueError("convergence_tolerance must be non-negative.")
        self.model = model
        self.outdoor_temperature = float(outdoor_temperature)
        self.heat_loads = heat_loads
        self.max_iterations = int(max_iterations)
        self.convergence_tolerance = float(convergence_tolerance)
        self.flow_multiplier = float(flow_multiplier)
        self.paths = paths
        self.work_dir = work_dir
        self.contam_exe = contam_exe
        self.simread_exe = simread_exe
        self.response_file = response_file
        self.wind_direction_vector = wind_direction_vector
        self.wind_speed = float(wind_speed)
        self.indoor_temperature = indoor_temperature
        self.thermal = bool(thermal)
        self.alpha = float(alpha)
        self.network_dict = network_dict
        if not np.isfinite(self.wind_speed) or self.wind_speed < 0:
            raise ValueError("wind_speed must be finite and non-negative")

    def run(self) -> AirflowResult:
        """Build the project and iterate airflow and zone sensible heat balance."""
        if self.paths is not None:
            return self._run()

        with SimulationWorkspace(parent=self.work_dir, prefix="moosas-airflow-", retain=self.work_dir is not None) as workspace:
            self.paths = VentPaths.from_workspace(workspace.path)
            try:
                return self._run(workspace.report)
            finally:
                self.paths = None

    def _run(self, workspace_report=None) -> AirflowResult:
        network_file = os.path.join(self.paths.project_dir, "model.json")
        project_file = os.path.join(self.paths.project_dir, "model.prj")
        if self.network_dict is None:
            zones, airflow_paths = getZoneAndPath(
                self.model, calculate_heat=self.thermal and self.heat_loads is None)
        else:
            zones = [AfnZone(**dict(value)) for value in self.network_dict.get("zones", {}).values()]
            airflow_paths = [AfnPath(**dict(value)) for value in self.network_dict.get("paths", {}).values()]
            if not zones:
                raise ValueError("network_dict contains no airflow zones")
        zones, airflow_paths = self._apply_heat_loads(zones, airflow_paths)
        if all(hasattr(z, "prjIndex") for z in zones) and all(
                hasattr(p, "fromZone") and hasattr(p, "toZone") for p in airflow_paths):
            airflow_paths, zones = cleanseNetwork(airflow_paths, zones)
        if not zones:
            raise ValueError("No airflow zones connected to ambient")
        for zone in zones:
            if self.indoor_temperature is not None:
                if isinstance(self.indoor_temperature, dict):
                    zone.temperature = float(self.indoor_temperature.get(str(zone.userName), zone.temperature))
                else:
                    zone.temperature = float(self.indoor_temperature)
            if not np.isfinite(zone.volume) or zone.volume <= 0:
                raise ValueError(f"Invalid zone volume: {zone.userName}")
        if self.wind_direction_vector is not None and self.wind_speed > 0:
            direction = np.asarray(self.wind_direction_vector, dtype=float)
            if direction.shape != (3,) or not np.all(np.isfinite(direction)) or np.linalg.norm(direction) == 0:
                raise ValueError("wind_direction_vector must be a finite nonzero 3-vector")
            direction /= np.linalg.norm(direction)
            applyWindPressure(airflow_paths, Vector(direction.tolist()), speed=self.wind_speed, alpha=self.alpha)
        buildNetworkFile(
            model=self.model,
            pathList=airflow_paths,
            zoneList=zones,
            networkFilePath=network_file,
        )
        platform_dir, suffix = native_platform()
        afn_result = self.run_command((
            os.path.join(path.libDir, "moosas_afn", platform_dir, "bin", f"MoosasAFN{suffix}"),
            "-p", "model",
            "-d", self.paths.project_dir,
            "-t", str(self.outdoor_temperature),
            "-s", "0",
            network_file,
        ))
        commands = [afn_result]
        if self.indoor_temperature is not None:
            # The network generator formats temperatures to two decimals.
            # Restore the full Picard iterate before computing buoyancy.
            _write_project_temperatures(
                np.asarray([z.temperature + 273.15 for z in zones]), project_file)
        temperature_history = [[] for _ in zones]
        ach_history = [[] for _ in zones]
        previous_temperature = None
        previous_ach = None
        residual = float("inf")
        airflow_matrix = None
        mass_flow_matrix = None

        for iteration_count in range(1, self.max_iterations + 1):
            iteration_result = self._run_project(project_file)
            commands.extend(iteration_result.commands)
            airflow_matrix = np.asarray(iteration_result.airflow_matrix, dtype=float) * self.flow_multiplier
            raw_mass_flow = iteration_result.mass_flow_matrix_kg_s
            if raw_mass_flow is None:
                raw_mass_flow = np.asarray(iteration_result.airflow_matrix, dtype=float) * AIR_DENSITY / 3600.0
            mass_flow_matrix = np.asarray(raw_mass_flow, dtype=float) * self.flow_multiplier
            temperature = _solve_sensible_heat(
                airflow_matrix.copy(),
                np.array([zone.heatLoad for zone in zones]),
                self.outdoor_temperature,
            ) if self.thermal else np.array([zone.temperature + 273.15 for zone in zones])
            temperature_values = (np.asarray(temperature, dtype=float) - 273.15).flatten()
            ach_values = airflow_matrix[-1, :-1] / np.array([zone.volume for zone in zones])
            for index in range(len(zones)):
                temperature_history[index].append(float(temperature_values[index]))
                ach_history[index].append(float(ach_values[index]))

            if previous_temperature is not None:
                temperature_residual = _relative_residual(temperature_values, previous_temperature)
                ach_residual = _relative_residual(ach_values, previous_ach)
                residual = float(np.mean(np.concatenate((temperature_residual, ach_residual))))

            if not self.thermal:
                residual = 0.0
                break
            if residual <= self.convergence_tolerance:
                break
            if iteration_count < self.max_iterations:
                _write_project_temperatures(temperature, project_file)
            previous_temperature = temperature_values
            previous_ach = ach_values

        converged = residual <= self.convergence_tolerance
        return AirflowResult(
            airflow_matrix=airflow_matrix,
            zones=tuple(
                AirflowZoneResult(
                    user_name=zone.userName,
                    project_name=zone.prjName,
                    heat_load=float(zone.heatLoad),
                    volume=float(zone.volume),
                    temperatures=tuple(temperature_history[index]),
                    ach_values=tuple(ach_history[index]),
                )
                for index, zone in enumerate(zones)
            ),
            converged=converged,
            iteration_count=iteration_count,
            residual=residual,
            path_results=tuple(_path_results(project_file, airflow_paths, zones, self.flow_multiplier))
            if all(hasattr(p, "fromZone") and hasattr(p, "pathWidth") for p in airflow_paths) else (),
            mass_flow_matrix_kg_s=mass_flow_matrix,
            commands=tuple(commands),
            warnings=() if converged else ("Airflow iteration did not converge.",),
            workspace=workspace_report or WorkspaceReport(self.paths.workspace, True),
        )

    def _apply_heat_loads(self, zones, airflow_paths):
        if self.heat_loads is None:
            return zones, airflow_paths
        known_zones = {zone.userName for zone in zones}
        unknown_zones = set(self.heat_loads) - known_zones
        if unknown_zones:
            raise ValueError(f"Unknown airflow zones: {sorted(unknown_zones)}")
        old_index_by_name = {zone.userName: int(zone.prjIndex) for zone in zones}
        zones = [zone for zone in zones if zone.userName in self.heat_loads]
        if not zones:
            raise ValueError("heat_loads must select at least one airflow zone.")
        index_map = {}
        for index, zone in enumerate(zones, start=1):
            index_map[old_index_by_name[zone.userName]] = index
            zone.prjIndex = index
            zone.prjName = f"z{index:03d}"
            zone.heatLoad = float(self.heat_loads[zone.userName])
        kept_paths = []
        for item in airflow_paths:
            source, target = int(item.fromZone), int(item.toZone)
            if source != -1 and source not in index_map or target != -1 and target not in index_map:
                continue
            item.fromZone = -1 if source == -1 else index_map[source]
            item.toZone = -1 if target == -1 else index_map[target]
            kept_paths.append(item)
        for index, item in enumerate(kept_paths, start=1):
            item.prjIndex = index
            if isinstance(item.element, dict):
                item.element["prjIndex"] = index
                item.element["userName"] = str(item.userName)
        return zones, kept_paths

    def _run_project(self, project_file):
        contam_exe = self.contam_exe or self.paths.contamx
        simread_exe = self.simread_exe or self.paths.simread
        response_path = self.response_file or self.paths.response
        contam_result = self.run_command((contam_exe, project_file))
        with open(response_path, "r", encoding="utf-8") as response_file:
            simread_result = self.run_command(
                (simread_exe, project_file),
                stdin=response_file,
            )
        airflow_matrix = build_matrix(file_path=project_file)
        try:
            mass_flow = build_mass_matrix(file_path=project_file)
        except (FileNotFoundError, OSError):
            # Supports test/native adapters that provide only the legacy matrix.
            mass_flow = np.asarray(airflow_matrix, dtype=float) * AIR_DENSITY / 3600.0
        return AirflowResult(
            airflow_matrix=airflow_matrix,
            mass_flow_matrix_kg_s=mass_flow,
            commands=(contam_result, simread_result),
            workspace=WorkspaceReport(self.paths.workspace, True),
        )


def _path_results(project_file, paths, zones, multiplier=1.0):
    flows = read_flowpath(project_file[:-4] + ".lfr") * (3600.0 / AIR_DENSITY) * multiplier
    topology = read_topology(project_file)
    ordered = sorted({path.userName: path for path in paths}.values(), key=lambda p: p.prjIndex)
    if len(flows) != len(ordered) or len(topology) != len(ordered):
        raise ValueError("CONTAM path count does not match the RDF network")
    names = {z.prjIndex: str(z.userName) for z in zones}
    names[-1] = "ambient"
    positions = {z.prjIndex: np.array([z.position_x, z.position_y, z.position_z]) for z in zones}
    for index, item in enumerate(ordered):
        source, target = map(int, topology[index])
        if (source, target) != (item.fromZone, item.toZone):
            raise ValueError("CONTAM path topology does not match the RDF network")
        forward = float(np.maximum(flows[index], 0).sum())
        reverse = float(-np.minimum(flows[index], 0).sum())
        position = np.array([item.position_x, item.position_y, item.position_z])
        delta = positions.get(target, position) - positions.get(source, position)
        direction = np.asarray(item.orientation, dtype=float)
        norm = np.linalg.norm(direction)
        if norm > 0:
            direction = direction / norm
            if np.dot(direction, delta) < 0:
                direction = -direction
        area = float(item.pathWidth * item.pathHeight)
        yield {"uid": str(item.userName), "from": names[source], "to": names[target],
               "forward_m3_h": forward, "reverse_m3_h": reverse,
               "direction": direction.tolist(), "area_m2": area,
               "forward_velocity_m_s": forward / (3600 * area) if area > 0 else 0.0,
               "reverse_velocity_m_s": reverse / (3600 * area) if area > 0 else 0.0,
               "kind": item.pathType}


def _relative_residual(current, previous):
    denominator = np.maximum(np.abs(previous), np.finfo(float).eps)
    return np.abs((current - previous) / denominator)


def _solve_sensible_heat(airflow_matrix, heat_loads, outdoor_temperature):
    temperature_k = _calculate_temperature(airflow_matrix, heat_loads, outdoor_temperature)
    temperature_c = np.asarray(temperature_k, dtype=float) - 273.15
    temperature_c = np.where(temperature_c < 10.0, 22.0, temperature_c)
    temperature_c = np.where(temperature_c > 30.0, 30.0, temperature_c)
    return temperature_c + 273.15


def _write_project_temperatures(temperature, project_file):
    head, temperature_block, rear = read_file(project_file)
    rows = np.array([
        re.split(r"[ ]+", line)
        for line in temperature_block.split("\n")[:-1]
    ], dtype=object)
    rows[:, 9] = np.asarray(temperature, dtype=float).astype(str)
    temperature_block = "\n".join(" ".join(row) for row in rows) + "\n"
    write_file(project_file, head, temperature_block, rear)


def _calculate_temperature(airflow_matrix, heat_loads, outdoor_temperature):
    airflow_matrix = np.array(airflow_matrix, dtype=float, copy=True)
    heat_loads = np.asarray(heat_loads, dtype=float).reshape(-1)
    for column in range(len(airflow_matrix)):
        airflow_matrix[column, column] = -np.sum(airflow_matrix[:, column])
        for row in range(len(airflow_matrix) - 1):
            if airflow_matrix[row, column] == 0:
                airflow_matrix[row, column] = -0.0001
    outdoor_heat = airflow_matrix[-1, :-1] * (outdoor_temperature * 1.2 / 3600 * 1005)
    enthalpy = heat_loads + outdoor_heat
    airflow_matrix *= 1.2 / 3600 * 1005
    return 273.15 - np.linalg.solve(airflow_matrix[:-1, :-1].T, enthalpy)


def read_path_result(project_file):
    """Return volumetric flow by path from a completed CONTAM project."""
    airflow = read_flowpath(project_file[:-4] + ".lfr") * 3600.0 / AIR_DENSITY
    topology = read_topology(project_file)
    return {
        index: {
            "from": topology[index][0],
            "to": topology[index][1],
            "flow": airflow[index][0] + airflow[index][1],
        }
        for index in range(len(topology))
    }
