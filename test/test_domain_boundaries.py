import ast
import inspect
from pathlib import Path
import textwrap
from types import SimpleNamespace
import unittest

import numpy as np

from MoosasPy.simulation.airflow.network import AfnZone
from MoosasPy.simulation.coupling.energy_airflow import EnergyAirflowCoupler
from MoosasPy.simulation.energy.pv import calculate_pv_generation
from MoosasPy.simulation.energy.runner import build_energy_input


class DomainBoundaryTests(unittest.TestCase):
    def test_domains_do_not_import_parallel_simulation_packages(self):
        simulation_dir = Path(__file__).parents[1] / "MoosasPy" / "simulation"
        domain_names = {"energy", "radiation", "airflow", "weather"}
        violations = []

        for domain_name in ("energy", "radiation", "airflow"):
            forbidden_names = domain_names - {domain_name}
            for source_path in (simulation_dir / domain_name).rglob("*.py"):
                tree = ast.parse(source_path.read_text(encoding="utf-8-sig"), filename=str(source_path))
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom):
                        module_parts = (node.module or "").split(".")
                        relative_sibling = (
                            node.level == 2
                            and module_parts
                            and module_parts[0] in forbidden_names
                        )
                        relative_simulation_sibling = (
                            node.level >= 3
                            and len(module_parts) >= 2
                            and module_parts[0] == "simulation"
                            and module_parts[1] in forbidden_names
                        )
                        absolute_sibling = (
                            len(module_parts) >= 3
                            and module_parts[:2] == ["MoosasPy", "simulation"]
                            and module_parts[2] in forbidden_names
                        )
                        if relative_sibling or relative_simulation_sibling or absolute_sibling:
                            violations.append(f"{source_path}:{node.lineno} -> {node.module}")
                    elif isinstance(node, ast.Import):
                        for alias in node.names:
                            module_parts = alias.name.split(".")
                            if (
                                len(module_parts) >= 3
                                and module_parts[:2] == ["MoosasPy", "simulation"]
                                and module_parts[2] in forbidden_names
                            ):
                                violations.append(f"{source_path}:{node.lineno} -> {alias.name}")

        self.assertEqual(violations, [])

    def test_pv_conversion_is_owned_by_energy_and_uses_explicit_input(self):
        result = calculate_pv_generation(
            [100.0, 200.0],
            useful_area_ratio=0.5,
            efficiency=0.2,
        )

        np.testing.assert_allclose(result, [10.0, 20.0])

    def test_energy_requires_precomputed_radiation(self):
        model = SimpleNamespace(
            spaceList=[SimpleNamespace(id="zone-1", settings={})],
        )
        weather = SimpleNamespace()

        with self.assertRaisesRegex(ValueError, "irradiance_by_surface"):
            build_energy_input(model, weather, require_radiation=True)

    def test_energy_requires_explicit_weather(self):
        model = SimpleNamespace(spaceList=[], schedule=None)

        with self.assertRaises(TypeError):
            build_energy_input(model)

    def test_airflow_requires_precomputed_radiation(self):
        space = SimpleNamespace(id="zone-1", settings={})

        with self.assertRaisesRegex(ValueError, "Precomputed"):
            AfnZone.fromElement(space)

    def test_coupled_task_does_not_swallow_simulation_failures(self):
        tree = ast.parse(textwrap.dedent(inspect.getsource(EnergyAirflowCoupler.coupledTask)))

        self.assertFalse(any(isinstance(node, ast.ExceptHandler) for node in ast.walk(tree)))


if __name__ == "__main__":
    unittest.main()
