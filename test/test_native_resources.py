from __future__ import annotations

import csv
import inspect
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from eppy.modeleditor import IDF
import pytest
import numpy as np

from MoosasPy.model.io.idf.version import (
    ENERGYPLUS_VERSION,
    bundled_idd_path,
    bundled_template_idf_path,
    configure_idd,
    idd_version,
    idf_version,
    require_idf_version,
)
from MoosasPy.simulation.airflow import create_openfoam_workspace
from MoosasPy.simulation.weather import write_epw_csv
from MoosasPy.utils.tools import path, native_platform


@pytest.mark.parametrize("platform_dir,suffix", [("linux-x86_64", ""), ("windows-x86_64", ".exe")])
def test_native_runtimes_have_one_platform_layout(platform_dir, suffix):
    root = Path(path.libDir)
    tools = {
        "radiance": ("epw2wea", "gendaylit", "gendaymtx", "gensky", "oconv", "rcontrib", "rfluxmtx", "rtrace"),
        "contam": ("contamx3", "simread"),
        "moosas_rad": ("MoosasRad",),
        "moosas_afn": ("MoosasAFN",),
        "moosas_energy": ("MoosasEnergy",),
    }
    for tool, programs in tools.items():
        for program in programs:
            file = root / tool / platform_dir / "bin" / (program + suffix)
            assert file.is_file(), file
            with file.open("rb") as stream:
                magic = stream.read(20)
            if suffix:
                assert magic[:2] == b"MZ"
            else:
                assert magic[:5] == b"\x7fELF\x02"
                assert int.from_bytes(magic[18:20], "little") == 62
    for file in ("perezlum.cal", "rayinit.cal", "reinhartb.cal", "skybright.cal"):
        assert (root / "radiance" / platform_dir / "lib" / file).is_file()
    assert (root / "contam" / "response.txt").is_file()
    if suffix:
        assert (root / "radiance" / platform_dir / "bin" / "vcruntime140.dll").is_file()


def test_native_runtime_tree_excludes_outputs_and_old_paths():
    root = Path(path.libDir)
    assert {file.name for file in root.iterdir()} == {
        "README.md", "radiance", "contam", "moosas_rad", "moosas_afn", "moosas_energy",
    }
    for file in root.rglob("*"):
        if file.is_file():
            assert file.suffix not in {".bat", ".prj", ".rad", ".input", ".csv", ".json"}, file
    assert (Path(path.dataBaseDir) / "radiation" / "sun_position.csv").is_file()
    assert (Path(path.dataBaseDir) / "airflow" / "xgb.json").is_file()


def test_scene_writers_require_workspace_output_paths():
    from MoosasPy.simulation.radiation.scene import _model_to_radiance, _space_to_radiance, _write_grid

    for function, parameter in ((_model_to_radiance, "rad_path"),
                                (_space_to_radiance, "rad_path"), (_write_grid, "grid_path")):
        assert inspect.signature(function).parameters[parameter].default is inspect.Parameter.empty


def test_radiance_and_weather_share_one_tool_installation(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from MoosasPy.simulation.radiation import RadianceRunner
    from MoosasPy.simulation.weather import generate_cumulative_sky
    from MoosasPy.simulation.weather.data import WeatherData
    from MoosasPy.simulation.runner import Runner

    commands = []
    def run_command(self, command, **options):
        commands.append(command)
        if options.get("stdout") is not None:
            options["stdout"].write("1 1 1\n" * 146)
        else:
            Path(command[-1]).write_text("wea")

    monkeypatch.setattr(Runner, "run_command", run_command)
    monkeypatch.setattr(WeatherData, "HOURS_PER_YEAR", 1)
    wea = tmp_path / "weather.wea"
    wea.write_text("weather", encoding="utf-8")
    generate_cumulative_sky(wea)
    runner = RadianceRunner(SimpleNamespace())
    assert Path(commands[0][0]) == runner._executable("gendaymtx")


def test_relocated_sky_directions_and_pressure_model_are_usable():
    from MoosasPy.simulation.radiation.calculation import _default_sky_positions
    from MoosasPy.simulation.weather import CumulativeSky
    from MoosasPy.simulation.airflow.pressure_model import callXgb

    expected = _default_sky_positions()
    actual = CumulativeSky(np.ones(145)).positions
    assert len(expected) == len(actual) == 145
    np.testing.assert_array_equal([point.array for point in expected], [point.array for point in actual])
    prediction = callXgb([[1., 1., 0., .5, .5]])
    assert prediction.shape == (1,) and np.isfinite(prediction).all()


def test_native_sources_and_build_scripts_live_with_their_tools():
    root = Path(path.libDir)
    assert not (Path(__file__).parents[1] / "native").exists()
    for tool, files in (
        ("moosas_rad", ("MoosasRad.go", "MoosasBVH.go", "build.sh")),
        ("moosas_afn", ("MoosasAFN.go", "build.sh")),
    ):
        for file in files:
            assert (root / tool / file).is_file()


def test_bundled_energyplus_resources_are_26_1():
    assert ENERGYPLUS_VERSION == "26.1"
    assert idd_version(bundled_idd_path()) == "26.1"
    assert idf_version(bundled_template_idf_path()) == "26.1"

    configure_idd()
    template = IDF(str(bundled_template_idf_path()))
    assert str(template.idfobjects["VERSION"][0].Version_Identifier) == "26.1"


def test_old_idf_is_rejected_before_parsing(tmp_path):
    old_idf = tmp_path / "old.idf"
    old_idf.write_text("Version,24.2;\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"requires EnergyPlus 26\.1 IDF input.*got 24\.2"):
        require_idf_version(old_idf)


class NativeToolResourceTests(unittest.TestCase):
    def test_create_openfoam_workspace_replaces_existing_content(self):
        with TemporaryDirectory() as directory:
            workspace = Path(directory) / "case"
            workspace.mkdir()
            (workspace / "stale.txt").write_text("stale", encoding="utf-8")

            self.assertEqual(create_openfoam_workspace(workspace), workspace)

            self.assertFalse((workspace / "stale.txt").exists())
            for relative_path in ("0", "constant/triSurface", "log", "system"):
                self.assertTrue((workspace / relative_path).is_dir())
            self.assertTrue((workspace / "vent.foam").is_file())

    def test_write_epw_csv_matches_dest_format(self):
        with TemporaryDirectory() as directory:
            epw_path = Path(directory) / "weather.epw"
            output_path = Path(directory) / "weather.csv"
            self._write_epw_fixture(epw_path)

            self.assertEqual(write_epw_csv(epw_path, output_path), str(output_path))

            with output_path.open(newline="", encoding="utf-8") as output_file:
                rows = list(csv.reader(output_file))

        self.assertEqual(len(rows), 8760)
        # 10 C dew point at 101325 Pa: 7.62 g/kg, retaining DeST CSV units.
        self.assertEqual(rows[0], [
            "12345", "0", "0", "20.13", "7.62", "100.00", "50.00",
            "10.00", "269.70", "1.00", "16", "101325.00", "9999999",
        ])

    @staticmethod
    def _write_epw_fixture(epw_path: Path) -> None:
        ground_temperatures = ["GROUND", "0", "0", "0", "0", "0"] + ["10"] * 12
        hourly_record = ["0"] * 22
        hourly_record[6] = "20.125"
        hourly_record[7] = "10"
        hourly_record[9] = "101325"
        hourly_record[12] = "300"
        hourly_record[13] = "100"
        hourly_record[15] = "50"
        hourly_record[20] = "0"
        hourly_record[21] = "1"
        lines = [
            "LOCATION,City,State,Country,Source,12345;,1,2,3,4",
            "header",
            "header",
            ",".join(ground_temperatures),
            "header",
            "header",
            "header",
            "header",
        ] + [",".join(hourly_record)] * 8760
        epw_path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()