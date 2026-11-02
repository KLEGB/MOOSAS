from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import math
import unittest

from MoosasPy.simulation.energy.runner import EnergyRunner
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
    def test_energy_engine_returns_real_results_for_geometry_fixture(self):
        weather = read_weather_csv(
            BEIJING_WEATHER,
            Location("545110", "BEIJING/PEKING", "-", 39.93, 116.28, 55.0, 100962.17),
        )

        result = EnergyRunner(model=self.model, weather=weather).run()

        self.assertEqual(len(result.commands), 1)
        self.assertEqual(result.commands[0].returncode, 0)
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


if __name__ == "__main__":
    unittest.main()
