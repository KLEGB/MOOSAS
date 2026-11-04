from datetime import datetime
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from MoosasPy.simulation import SimulationWorkspace
from MoosasPy.simulation.airflow.parser import read_file, read_topology
from MoosasPy.simulation.airflow.runner import (
    AirflowResult,
    AirflowRunner,
    VentPaths,
)
from MoosasPy.simulation.engine import NativeExecution
from MoosasPy.simulation.radiation.calculation import (
    calculate_model_radiation,
    calculate_position_radiation,
    ray_test,
    write_radiation_geometry,
)
from MoosasPy.simulation.radiation.runner import (
    RadianceCommandError,
    RadianceRunner,
    RadianceSky,
    RadianceTimeoutError,
)
from MoosasPy.simulation.radiation.sunlight import calculate_position_sun_hours
from MoosasPy.simulation.runner import CommandError, CommandTimeoutError, Runner
from MoosasPy.simulation.weather import Location
from MoosasPy.simulation.weather.epw import convert_epw_to_wea
from MoosasPy.transform.geometry.geos import Ray, Vector
from MoosasPy.utils.constant import rad
from MoosasPy.utils.tools import native_platform


class RecordingEngine:
    name = "recording"

    def __init__(self):
        self.calls = []

    def execute(self, command, **options):
        self.calls.append((command, options))
        return NativeExecution(command, 0, "engine output", "")


class SimulationFoundationTests(unittest.TestCase):
    def test_runner_accepts_a_replaceable_native_engine(self):
        engine = RecordingEngine()

        result = Runner(engine=engine, timeout_seconds=17).run_command(
            ["native-tool", 42], cwd="workspace"
        )

        self.assertEqual(result.command, ("native-tool", "42"))
        self.assertEqual(result.stdout, "engine output")
        self.assertEqual(engine.calls[0][1]["timeout_seconds"], 17)

    def test_temporary_workspace_is_cleaned_and_reported(self):
        with TemporaryDirectory() as parent:
            with SimulationWorkspace(parent=parent, prefix="test-run-") as workspace:
                artifact = workspace.child("output", "result.txt")
                artifact.write_text("result", encoding="utf-8")
                report = workspace.report
                self.assertTrue(artifact.exists())
                self.assertFalse(report.retained)

            self.assertFalse(Path(report.path).exists())

    def test_retained_workspace_survives_context_exit(self):
        with TemporaryDirectory() as parent:
            with SimulationWorkspace(parent=parent, prefix="test-run-", retain=True) as workspace:
                report = workspace.report

            self.assertTrue(Path(report.path).is_dir())
            self.assertTrue(report.retained)


class RunnerTests(unittest.TestCase):
    def test_success_returns_structured_diagnostics(self):
        command = ("native-tool", "--version")
        completed = subprocess.CompletedProcess(command, 0, stdout="ready", stderr="warning")

        with patch("MoosasPy.simulation.runner.subprocess.run", return_value=completed):
            result = Runner(timeout_seconds=12).run_command(command, cwd="workdir")

        self.assertEqual(result.command, command)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "ready")
        self.assertEqual(result.stderr, "warning")

    def test_nonzero_exit_preserves_diagnostics(self):
        command = ("native-tool", "input")
        completed = subprocess.CompletedProcess(command, 4, stdout="partial", stderr="bad input")

        with patch("MoosasPy.simulation.runner.subprocess.run", return_value=completed):
            with self.assertRaises(CommandError) as error:
                Runner().run_command(command)

        self.assertEqual(error.exception.returncode, 4)
        self.assertEqual(error.exception.stdout, "partial")
        self.assertEqual(error.exception.stderr, "bad input")

    def test_timeout_preserves_diagnostics(self):
        command = ("native-tool", "input")
        timeout = subprocess.TimeoutExpired(command, 3, output=b"partial", stderr=b"timed out")

        with patch("MoosasPy.simulation.runner.subprocess.run", side_effect=timeout):
            with self.assertRaises(CommandTimeoutError) as error:
                Runner(timeout_seconds=3).run_command(command)

        self.assertEqual(error.exception.stdout, "partial")
        self.assertEqual(error.exception.stderr, "timed out")

    def test_file_streams_are_passed_without_shell_redirection(self):
        command = ("native-tool", "input")

        def run_command(*_args, stdin, stdout, **_kwargs):
            self.assertIsNotNone(stdin)
            self.assertIsNotNone(stdout)
            stdout.write("result\n")
            return subprocess.CompletedProcess(command, 0, stdout=None, stderr="")

        with TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            input_path = root / "input.txt"
            output_path = root / "output.txt"
            input_path.write_text("request\n", encoding="utf-8")
            with input_path.open("r", encoding="utf-8") as input_file, output_path.open(
                "w", encoding="utf-8"
            ) as output_file, patch(
                "MoosasPy.simulation.runner.subprocess.run", side_effect=run_command
            ):
                Runner().run_command(command, stdin=input_file, stdout=output_file)

            self.assertEqual(output_path.read_text(encoding="utf-8"), "result\n")


class SimulationWorkspaceTests(unittest.TestCase):
    def test_native_platform_selects_supported_x86_64_binaries(self):
        self.assertEqual(native_platform("win32", "AMD64"), ("windows-x86_64", ".exe"))
        self.assertEqual(native_platform("linux", "x86_64"), ("linux-x86_64", ""))

    def test_native_platform_rejects_unsupported_hosts(self):
        with self.assertRaisesRegex(OSError, "architecture"):
            native_platform("linux", "aarch64")
        with self.assertRaisesRegex(OSError, "platform"):
            native_platform("darwin", "x86_64")

    def test_weather_conversion_uses_explicit_output_path(self):
        def run_command(command, **_kwargs):
            Path(command[-1]).write_text("wea", encoding="utf-8")

        with TemporaryDirectory() as work_dir, patch(
            "MoosasPy.simulation.weather.epw.Runner.run_command", side_effect=run_command
        ):
            output_path = Path(work_dir) / "station.wea"
            wea_path = Path(convert_epw_to_wea("input.epw", str(output_path)))

            self.assertEqual(wea_path, output_path)
            self.assertTrue(wea_path.exists())

    def test_airflow_default_workspace_is_owned_by_run(self):
        observed_paths = []

        def run_in_workspace(runner, report):
            observed_paths.append(Path(report.path))
            self.assertTrue(observed_paths[-1].is_dir())
            return AirflowResult(workspace=report)

        with TemporaryDirectory() as work_dir, patch.object(
            VentPaths,
            "from_workspace",
            side_effect=lambda root: SimpleNamespace(workspace=str(root)),
        ) as from_workspace, patch.object(
            AirflowRunner,
            "_run",
            autospec=True,
            side_effect=run_in_workspace,
        ):
            runner = AirflowRunner(model=SimpleNamespace(), work_dir=work_dir)
            self.assertIsNone(runner.paths)
            result = runner.run()

        self.assertFalse(result.workspace.retained)
        self.assertFalse(observed_paths[0].exists())
        from_workspace.assert_called_once()

    def test_contam_parsers_reject_sections_without_terminator(self):
        with TemporaryDirectory() as work_dir:
            project_path = Path(work_dir) / "broken.prj"
            project_path.write_text("zones\nheader\nzone row\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Missing -999 terminator"):
                read_file(project_path)

            project_path.write_text("flow paths\nheader\npath row\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Missing -999 terminator"):
                read_topology(project_path)

    def test_radiation_and_vent_defaults_use_unique_workspaces(self):
        with TemporaryDirectory() as work_dir, patch(
            "MoosasPy.simulation.radiation.calculation.writeGeo"
        ) as write_geo:
            geo_path = Path(write_radiation_geometry(SimpleNamespace(), work_dir=work_dir))
            write_geo.assert_called_once_with(str(geo_path), unittest.mock.ANY)
            self.assertEqual(geo_path.name, "model.geo")
            self.assertEqual(geo_path.parent.parent, Path(work_dir))

            vent_paths = VentPaths.create(work_dir=work_dir)
            self.assertEqual(Path(vent_paths.workspace).parent, Path(work_dir))
            self.assertTrue(Path(vent_paths.project_dir).is_dir())
            self.assertTrue(Path(vent_paths.result_dir).is_dir())
            self.assertTrue(Path(vent_paths.contamx).is_file())
            self.assertTrue(Path(vent_paths.simread).is_file())
            self.assertEqual(Path(vent_paths.contamx).parent, Path(vent_paths.workspace) / "contam")
            self.assertEqual(Path(vent_paths.response).name, "response.txt")

    def test_position_radiation_preserves_energy_across_reflections(self):
        position_ray = Ray(Vector([0, 0, 0]), Vector([0, 0, 1]))
        sky = SimpleNamespace(
            positions=[Vector([0, 0, 1])],
            values=np.array([2.0]),
        )

        def reflect_once(rays, **_kwargs):
            if reflect_once.call_count == 0:
                reflect_once.call_count += 1
                return [Ray(ray.origin, ray.direction) if index == 0 else None for index, ray in enumerate(rays)]
            return [None] * len(rays)

        reflect_once.call_count = 0
        with patch(
            "MoosasPy.simulation.radiation.calculation.ray_test",
            side_effect=reflect_once,
        ):
            result = calculate_position_radiation(
                position_ray,
                sky,
                geo_path="model.geo",
                reflection=1,
            )

        self.assertAlmostEqual(result[0], 2.0 * rad.CONTENT_REFLECTION)

    def test_position_radiation_excludes_blocked_rays_without_reflections(self):
        position_ray = Ray(Vector([0, 0, 0]), Vector([0, 0, 1]))
        sky = SimpleNamespace(
            positions=[Vector([0, 0, 1])],
            values=np.array([2.0]),
        )

        with patch(
            "MoosasPy.simulation.radiation.calculation.ray_test",
            return_value=[Ray(position_ray.origin, position_ray.direction), None],
        ):
            result = calculate_position_radiation(
                position_ray,
                sky,
                geo_path="model.geo",
                reflection=0,
            )

        self.assertEqual(result[0], 0.0)

    def test_sun_hours_count_only_unobstructed_rays(self):
        position_ray = Ray(Vector([0, 0, 0]), Vector([0, 0, 1]))
        sky = SimpleNamespace(
            annual_sun=lambda leap_year: [Vector([0, 0, 1]), Vector([0, 0, 1])],
        )

        with patch(
            "MoosasPy.simulation.radiation.sunlight.ray_test",
            return_value=[None, Ray(position_ray.origin, position_ray.direction)],
        ):
            result = calculate_position_sun_hours(
                position_ray,
                sky,
                geo_path="model.geo",
                period_start=datetime(2026, 1, 1, 0),
                period_end=datetime(2026, 1, 2, 0),
            )

        self.assertEqual(result.tolist(), [1.0])

    def test_model_radiation_traces_shared_sky_geometry_once(self):
        class Glazing:
            normal = np.array([0.0, 0.0, 1.0])

            @staticmethod
            def getWeightCenter():
                return np.array([0.0, 0.0, 0.0])

            @staticmethod
            def area3d():
                return 1.0

        glazing = Glazing()
        space = SimpleNamespace(getAllFaces=lambda to_dict: [glazing], settings={})
        model = SimpleNamespace(spaceList=[space])
        positions = [Vector([0, 0, 1])]
        skies = {
            "summer": SimpleNamespace(positions=positions, values=np.array([2.0])),
            "winter": SimpleNamespace(positions=positions, values=np.array([3.0])),
        }

        with patch(
            "MoosasPy.simulation.radiation.calculation.write_radiation_geometry",
            return_value="model.geo",
        ), patch(
            "MoosasPy.simulation.radiation.calculation.MoosasGlazing",
            Glazing,
        ), patch(
            "MoosasPy.simulation.radiation.calculation.ray_test",
            return_value=[None, None],
        ) as ray_test_mock:
            calculate_model_radiation(model, skies, reflection=0)

        ray_test_mock.assert_called_once()

    def test_ray_test_cleans_its_internal_workspace(self):
        ray = Ray(Vector([0, 0, 0]), Vector([0, 0, 1]))

        def run_command(command, **_kwargs):
            Path(command[command.index("-o") + 1]).write_text(
                "-1,-1,-1,0,0,1\n",
                encoding="utf-8",
            )

        with TemporaryDirectory() as parent_dir, patch(
            "MoosasPy.simulation.radiation.calculation.tempfile.TemporaryDirectory",
            side_effect=lambda **kwargs: TemporaryDirectory(dir=parent_dir, **kwargs),
        ), patch(
            "MoosasPy.simulation.radiation.calculation.Runner.run_command",
            side_effect=run_command,
        ):
            self.assertEqual(ray_test([ray], geo_path="model.geo"), [None])
            self.assertEqual(list(Path(parent_dir).iterdir()), [])


class RadianceRunnerTests(unittest.TestCase):
    def setUp(self):
        location = Location("12345", "City", "State", 39.93, 116.28, 50, 101325)
        self.sky = RadianceSky(datetime(2026, 1, 1, 12), "-c", location, 15000)

    def test_run_uses_an_isolated_temporary_directory(self):
        floor = SimpleNamespace(Uid="floor-1")
        model = SimpleNamespace(
            spaceList=[SimpleNamespace(getAllFaces=lambda to_dict: {"MoosasFloor": [floor]})]
        )
        recorded_work_dirs = []

        def write_grid(_floor, grid_path, **_kwargs):
            Path(grid_path).write_text("0 0 0 0 0 1\n", encoding="utf-8")
            return ["0 0 0 0 0 1"]

        def run_command(command, cwd, **_kwargs):
            recorded_work_dirs.append(Path(cwd))
            if Path(command[0]).name.startswith("rtrace"):
                return subprocess.CompletedProcess(command, 0, stdout="1 1 1\n", stderr="")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with TemporaryDirectory() as parent_dir:
            with patch(
                "MoosasPy.simulation.radiation.runner._model_to_radiance"
            ) as model_to_rad, patch(
                "MoosasPy.simulation.radiation.runner._write_grid", side_effect=write_grid
            ), patch("MoosasPy.simulation.runner.subprocess.run", side_effect=run_command):
                result = RadianceRunner(model, self.sky, work_dir=parent_dir).run()

        self.assertEqual(len(result.floors), 1)
        self.assertAlmostEqual(result.floors[0].illuminances[0], 179.0)
        self.assertAlmostEqual(result.floors[0].daylight_factor, 179.0 / 15000)
        self.assertEqual(model_to_rad.call_args.args[3:5], (39.93, 116.28))
        self.assertTrue(recorded_work_dirs)
        self.assertFalse(recorded_work_dirs[0].exists())

    def test_nonzero_exit_preserves_stderr(self):
        runner = RadianceRunner(SimpleNamespace(), self.sky)
        command = ("rtrace", "model.oct")
        completed = subprocess.CompletedProcess(command, 3, stdout="", stderr="missing octree")

        with TemporaryDirectory() as run_dir, patch(
            "MoosasPy.simulation.runner.subprocess.run", return_value=completed
        ):
            with self.assertRaises(RadianceCommandError) as error:
                runner._run_command(command, Path(run_dir))

        self.assertEqual(error.exception.returncode, 3)
        self.assertEqual(error.exception.stderr, "missing octree")

    def test_timeout_preserves_command_diagnostics(self):
        runner = RadianceRunner(SimpleNamespace(), self.sky, timeout_seconds=1)
        command = ("rtrace", "model.oct")
        timeout = subprocess.TimeoutExpired(command, 1, output=b"partial output", stderr=b"still running")

        with TemporaryDirectory() as run_dir, patch(
            "MoosasPy.simulation.runner.subprocess.run", side_effect=timeout
        ):
            with self.assertRaises(RadianceTimeoutError) as error:
                runner._run_command(command, Path(run_dir))

        self.assertEqual(error.exception.stdout, "partial output")
        self.assertEqual(error.exception.stderr, "still running")


if __name__ == "__main__":
    unittest.main()