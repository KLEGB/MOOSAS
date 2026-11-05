from __future__ import annotations

import csv
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from eppy.modeleditor import IDF
import pytest

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