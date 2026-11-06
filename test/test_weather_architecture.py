from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np

from MoosasPy.simulation.weather import (
    CumulativeSky,
    DirectSky,
    WeatherData,
    build_cumulative_skies,
    generate_cumulative_sky,
    load_epw,
    write_epw_csv,
)
from MoosasPy.utils.date import DateTime


class WeatherArchitectureTests(unittest.TestCase):
    def test_epw_loads_as_typed_immutable_weather(self):
        with TemporaryDirectory() as temporary_dir:
            epw_path = Path(temporary_dir) / "weather.epw"
            self._write_epw_fixture(epw_path)
            weather = load_epw(epw_path, Path(temporary_dir) / "prepared")

        self.assertIsInstance(weather, WeatherData)
        self.assertEqual(weather.location.station_id, "12345")
        self.assertEqual(Path(weather.weather_file).name, "weather.csv")
        self.assertEqual(Path(weather.epw_file).name, "weather.epw")
        self.assertEqual(weather.temperature.shape, (8760,))
        self.assertTrue(np.issubdtype(weather.temperature.dtype, np.floating))
        self.assertFalse(weather.temperature.flags.writeable)
        with self.assertRaises(ValueError):
            weather.temperature[0] = 0

    def test_weather_humidity_ratio_is_si_from_csv_g_per_kg(self):
        with TemporaryDirectory() as temporary_dir:
            epw_path = Path(temporary_dir) / "weather.epw"
            self._write_epw_fixture(epw_path)
            weather = load_epw(epw_path, Path(temporary_dir) / "prepared")
            raw = np.loadtxt(weather.weather_file, delimiter=",")

        np.testing.assert_allclose(weather.humidity_ratio, raw[:, 4] / 1000, rtol=0, atol=0)
        self.assertTrue(0 < weather.humidity_ratio.max() < 0.05)

    def test_epw_cold_humidity_is_positive_and_pressure_dependent(self):
        with TemporaryDirectory() as temporary_dir:
            epw_path = Path(temporary_dir) / "cold.epw"
            self._write_epw_fixture(epw_path)
            lines = epw_path.read_text(encoding="utf-8").splitlines()
            for index in range(8, len(lines)):
                record = lines[index].split(",")
                record[7] = "-40"
                record[9] = "80000"
                lines[index] = ",".join(record)
            epw_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            csv_path = Path(temporary_dir) / "cold.csv"
            write_epw_csv(epw_path, csv_path)
            values = np.loadtxt(csv_path, delimiter=",")

        # g/kg at -40 C dew point and 80 kPa; the legacy polynomial went negative.
        self.assertTrue(np.all(values[:, 4] == 0.15))

    def test_cumulative_sky_builds_annual_and_seasonal_views(self):
        matrix = np.ones((145, CumulativeSky.HOURS_PER_YEAR))

        skies = build_cumulative_skies(matrix)

        self.assertEqual(set(skies), {"annual", "summer", "winter"})
        np.testing.assert_allclose(skies["annual"].values, 8.76)
        np.testing.assert_allclose(skies["summer"].values, 2.208)
        np.testing.assert_allclose(skies["winter"].values, 2.16)

    def test_generate_cumulative_sky_requests_matrix_output(self):
        with TemporaryDirectory() as temporary_dir, patch.object(
            WeatherData, "HOURS_PER_YEAR", 2
        ), patch(
            "MoosasPy.simulation.weather.epw.Runner.run_command"
        ) as run_command:
            wea_path = Path(temporary_dir) / "weather.wea"
            wea_path.write_text("weather", encoding="utf-8")
            run_command.side_effect = lambda command, stdout: stdout.write(
                "".join(f"{value} {value} {value}\n" * WeatherData.HOURS_PER_YEAR
                        for value in range(146))
            )

            matrix = generate_cumulative_sky(wea_path)

        command = run_command.call_args.args[0]
        self.assertEqual(command[1:-1], ["-m", "1", "-O1"])
        self.assertEqual(command[-1], str(wea_path.resolve()))
        self.assertEqual(matrix.shape, (145, 2))
        from MoosasPy.simulation.weather.epw import TREGENZA_COEFFICIENTS, TREGENZA_PATCHES_PER_ROW
        coefficients = np.repeat(TREGENZA_COEFFICIENTS, TREGENZA_PATCHES_PER_ROW)
        np.testing.assert_allclose(matrix[:, 0], np.arange(1, 146) * coefficients * 8760 / 1000)
        np.testing.assert_array_equal(matrix[:, 0], matrix[:, 1])

    def test_direct_sky_uses_degree_based_location_coordinates(self):
        sun = DirectSky(0, 0, time_zone=0).sun_at_datetime(DateTime(3, 20, 12))

        self.assertGreater(sun.z, 0.99)

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
