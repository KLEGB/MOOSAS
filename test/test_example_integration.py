from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import math
import unittest

import pytest

from MoosasPy.simulation.energy.runner import EnergyRunner, build_energy_input
from MoosasPy.simulation.weather import Location
from MoosasPy.simulation.weather.epw import read_weather_csv
from MoosasPy.transform import transform
from MoosasPy.transform.pipeline import _load_geometry_source
from MoosasPy.transform.stages.classification import classify_model
from MoosasPy.transform.stages.cleansing import cleanse_model
from MoosasPy.transform.stages.generation import CCRSpaceGeneration
from MoosasPy.model.io.xml import build_xml
from MoosasPy.transform.stages.splitting import split_wall_intersections
from MoosasPy.transform.stages.validation import validate_model
from MoosasPy.utils import np
from MoosasPy.utils.tools import native_platform, path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GEOMETRY_FIXTURE = PROJECT_ROOT / "test" / "caseFile" / "test8_topology.geo"
BEIJING_WEATHER = PROJECT_ROOT / "MoosasPy" / "db" / "weather" / "545110.csv"


class ExampleIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = transform(
            str(GEOMETRY_FIXTURE),
            input_type="geo",
            stdout=StringIO(),
        )

    def test_geometry_fixture_transforms_to_expected_topology(self):
        self.assertGreaterEqual(len(self.model.geometryList), 450)
        self.assertEqual(len(self.model.spaceList), 70)
        self.assertEqual(len(self.model.voidList), 0)
        self.assertEqual(len(self.model.wallList), 257)
        self.assertEqual(len(self.model.faceList), 153)
        self.assertEqual(len(self.model.glazingList), 33)
        self.assertEqual(len(self.model.skylightList), 0)
        self.assertEqual(
            tuple(self.model.levelList),
            (0.0, 4.5, 9.0, 13.5, 18.0, 22.5, 27.0, 31.5, 36.0, 40.5, 45.0),
        )
        self.assertTrue(all(space.area > 0 for space in self.model.spaceList))
        self.assertAlmostEqual(sum(space.area for space in self.model.spaceList), 28198.908, places=3)

        neighbor_pairs = {
            tuple(sorted((str(space.id), str(neighbor_id))))
            for space in self.model.spaceList
            for neighbor_id in space.neighbor
            if str(neighbor_id) != str(space.id)
        }
        self.assertEqual(len(neighbor_pairs), 122)
        self.assertTrue(all(space.neighbor for space in self.model.spaceList))

        residual_walls = set(self.model.wall_remain)
        active_air_walls = [
            wall
            for wall in self.model.wallList
            if wall.is_air_boundary and wall not in residual_walls
        ]
        self.assertEqual(len(active_air_walls), 36)
        self.assertEqual(
            sum(wall.is_air_boundary for wall in self.model.wall_remain),
            0,
        )
        self.assertTrue(all(len(set(wall.space)) == 2 for wall in active_air_walls))

    def test_classification_stage_builds_elements_before_topology(self):
        model = _load_geometry_source(str(GEOMETRY_FIXTURE), "geo")
        classified_model = classify_model(model)

        self.assertIs(classified_model, model)
        self.assertEqual(len(model.faceList), 144)
        self.assertEqual(len(model.wallList), 251)
        self.assertEqual(sum(wall.is_air_boundary for wall in model.wallList), 58)
        self.assertEqual(len(model.glazingList), 33)
        self.assertEqual(len(model.skylightList), 0)
        self.assertEqual(tuple(model.levelList), (0.0, 4.5, 9.0, 13.5, 18.0, 22.5, 27.0, 31.5, 36.0, 40.5, 45.0))

    def test_generation_stage_builds_boundaries_before_assembly(self):
        model = _load_geometry_source(str(GEOMETRY_FIXTURE), "geo")
        with redirect_stdout(StringIO()):
            model = classify_model(model)
            model.faceList = np.array(model.faceList)
            model.wallList = np.array(model.wallList)
            model.glazingList = np.array(model.glazingList)
            model, _ = cleanse_model(
                model,
                solve_duplicated=True,
                solve_redundant=True,
                solve_overlap=True,
            )
            model = split_wall_intersections(model, enabled=True)
            model = CCRSpaceGeneration(model)

        self.assertEqual(len(model.boundaryList), 70)
        self.assertEqual(len(model.edgeList), 0)
        self.assertEqual(len(model.spaceList), 0)

    def test_validation_rejects_an_unknown_neighbor(self):
        neighbors = self.model.spaceList[0].neighbor
        neighbors["missing-space"] = []
        try:
            with self.assertRaisesRegex(ValueError, "unknown neighbor"):
                validate_model(self.model)
        finally:
            del neighbors["missing-space"]

    def test_xml_serialization_is_owned_by_the_io_boundary(self):
        root = build_xml(self.model)

        self.assertFalse(hasattr(self.model, "buildXml"))
        self.assertEqual(root.tag, "model")
        self.assertEqual(len(root.findall("space")), len(self.model.spaceList))
        self.assertEqual(root.findtext("level"), " ".join(str(level) for level in self.model.levelList))

    @unittest.skipUnless(BEIJING_WEATHER.is_file(), "requires Beijing 545110 weather data")
    @pytest.mark.slow
    def test_energy_engine_returns_real_results_for_geometry_fixture(self):
        weather = read_weather_csv(
            BEIJING_WEATHER,
            Location("545110", "BEIJING/PEKING", "-", 39.93, 116.28, 55.0, 100962.17),
        )

        result = EnergyRunner(
            model=self.model,
            weather=weather,
            template_id="CN/GB55015-2021/residential_hot_summer_cold_winter_A/RESIDENTIAL",
        ).run()

        self.assertEqual(len(result.commands), 1)
        self.assertEqual(result.commands[0].returncode, 0)
        platform_dir, suffix = native_platform()
        self.assertEqual(
            Path(result.commands[0].command[0]),
            Path(path.libDir) / "moosas_energy" / platform_dir / "bin" / f"MoosasEnergy{suffix}",
        )
        self.assertEqual(len(result.data["spaces"]), 70)
        self.assertEqual(len(result.data["months"]), 12)

        total = result.data["total"]
        component_total = 0.0
        for key in ("cooling", "heating", "lighting", "equipment"):
            value = float(total[key])
            self.assertTrue(math.isfinite(value))
            self.assertGreaterEqual(value, 0.0)
            component_total += value
        self.assertGreater(component_total, 0.0)
        self.assertAlmostEqual(float(total["cooling"]), 6.75, places=2)
        self.assertAlmostEqual(float(total["heating"]), 48.64, places=2)
        self.assertAlmostEqual(float(total["lighting"]), 7.30, places=2)
        self.assertAlmostEqual(float(total["equipment"]), 19.42, places=2)
        self.assertAlmostEqual(float(total["total"]), component_total, places=6)

    @unittest.skipUnless(BEIJING_WEATHER.is_file(), "requires Beijing 545110 weather data")
    @pytest.mark.slow
    def test_latent_switch_preserves_sensible_results_and_adds_moisture(self):
        weather = read_weather_csv(
            BEIJING_WEATHER,
            Location("545110", "BEIJING/PEKING", "-", 39.93, 116.28, 55.0, 100962.17),
        )
        template_id = "CN/GB55015-2021/residential_hot_summer_cold_winter_A/RESIDENTIAL"
        space = self.model.spaceList[0]
        original_settings = dict(space.settings)
        original_explicit = list(space.explicit_settings)
        space.settings["zone_c_hum"] = 0.55
        space.explicit_settings = list(set(original_explicit) | {"zone_c_hum"})
        try:
            for start_hour in (744, 194 * 24):
                with self.subTest(start_hour=start_hour):
                    off = build_energy_input(self.model, weather, start_hour=start_hour,
                                             hours=24, template_id=template_id)
                    on = build_energy_input(self.model, weather, start_hour=start_hour,
                                            hours=24, template_id=template_id,
                                            latent={"enabled": True})
                    self.assertFalse(off.latent_enabled)
                    self.assertTrue(on.latent_enabled)
                    # Moisture is not thermal checkpoint state.
                    self.assertEqual(off.signature, on.signature)
                    self.assertEqual(off.hour(0), on.hour(0))

                    zone = next(z for z in on.zones if z.zone_id == str(space.id))
                    self.assertTrue(np.all(zone.cool_rh_fraction == 0.55))
                    np.testing.assert_allclose(
                        zone.fresh_air_m3_h + zone.infiltration_m3_h, zone.ventilation_m3_h
                    )
                    self.assertTrue(np.all(zone.people_moisture_kg_s >= 0))

                    sensible_rows, latent_rows = [], []
                    sensible = EnergyRunner(
                        energy_input=off, on_hour=lambda h, rows: sensible_rows.append(rows)
                    ).run()
                    latent = EnergyRunner(
                        energy_input=on, on_hour=lambda h, rows: latent_rows.append(rows)
                    ).run()

                    np.testing.assert_array_equal(
                        sensible.zone_temperature_c, latent.zone_temperature_c
                    )
                    np.testing.assert_array_equal(
                        sensible.zone_hourly_energy_kwh, latent.zone_hourly_energy_kwh
                    )
                    self.assertEqual(sensible.final_state, latent.final_state)
                    self.assertEqual(sensible.latent_load, {"enabled": False})
                    self.assertLess(latent.max_moisture_residual_kg_s, 1e-12)
                    self.assertFalse(latent.latent_load["equipment_model"])
                    self.assertFalse(latent.zone_latent_cooling_w.flags.writeable)
                    for hour, (before, after) in enumerate(zip(sensible_rows, latent_rows)):
                        for index, (old_row, new_row) in enumerate(zip(before, after)):
                            self.assertTrue(all(new_row[key] == value for key, value in old_row.items()))
                            self.assertAlmostEqual(
                                new_row["total_cooling_w"],
                                new_row["sensible_cooling_w"] + new_row["latent_cooling_w"],
                            )
                            np.testing.assert_allclose(
                                latent.zone_latent_cooling_w[index, hour],
                                new_row["latent_cooling_w"],
                            )
        finally:
            space.settings.clear()
            space.settings.update(original_settings)
            space.explicit_settings = original_explicit


if __name__ == "__main__":
    unittest.main()
