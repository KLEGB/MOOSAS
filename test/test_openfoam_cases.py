"""Real GEO to runnable OpenFOAM 12 cases."""

from io import StringIO
from pathlib import Path
import json
import shutil
import subprocess

import numpy as np
import pytest
import shapely

from MoosasPy.transform import transform
from MoosasPy.model.io.foam import exportFoam


@pytest.fixture(scope="module")
def building():
    return transform(str(Path(__file__).parent / "caseFile/test0_6spacesIntersection.geo"),
                     input_type="geo", stdout=StringIO())


@pytest.fixture(scope="module")
def indoor_model():
    return transform(str(Path(__file__).parent / "caseFile/test6_twoVolumes.geo"),
                     input_type="geo", stdout=StringIO())


def physical_conditions():
    return dict(viscosity=1.5e-5, turbulence_intensity=0.05,
                turbulence_length=1.0, iterations=1000)


def test_opening_in_one_part_of_a_multipart_wall():
    from MoosasPy.model import MoosasModel
    from MoosasPy.model.io._foam_case import _surface_triangles
    from MoosasPy.transform.geometry.element import MoosasElement

    model = MoosasModel()
    left = model.includeGeo(shapely.Polygon([(0, 0, 0), (2, 0, 0), (2, 0, 3), (0, 0, 3)]))
    right = model.includeGeo(shapely.Polygon([(2, 0, 0), (4, 0, 0), (4, 0, 3), (2, 0, 3)]))
    aperture = model.includeGeo(shapely.Polygon([(0.5, 0, 1), (1.5, 0, 1), (1.5, 0, 2), (0.5, 0, 2)]))
    opening = MoosasElement(model, aperture)
    wall = MoosasElement(model, [left, right], glazingElement=[opening])
    triangles = _surface_triangles(wall, {"inlet": opening})
    areas = {"inlet": 0.0, "walls": 0.0}
    for name, v in triangles:
        areas[name] += np.linalg.norm(np.cross(v[1] - v[0], v[2] - v[0])) / 2
    assert areas["inlet"] == pytest.approx(1.0)
    assert areas["walls"] == pytest.approx(11.0)


def indoor_conditions():
    return dict(**physical_conditions(),
                inlet={"opening": "gls_g_80", "velocity": [0.409451, 0.286966, 0]},
                outlet={"opening": "gls_g_79", "pressure": 0.0})


def outdoor_conditions():
    return dict(**physical_conditions(), velocity=[2.0, 0.0, 0.0],
                domain=[[-100, 120, 0], [0, 240, 45]],
                inside_point=[-90, 130, 5])


@pytest.mark.parametrize("scenario", ["indoor", "outdoor"])
def test_real_geo_saves_complete_case(building, indoor_model, tmp_path, scenario):
    model = indoor_model if scenario == "indoor" else building
    options = dict(space_index=39) if scenario == "indoor" else {}
    conditions = indoor_conditions() if scenario == "indoor" else outdoor_conditions()
    result = exportFoam(model, tmp_path / "case.foam", scenario=scenario, grid_size=2,
                        conditions=conditions, **options)
    assert result.primary_path == tmp_path / "case.foam"
    for name in ("system/blockMeshDict", "system/snappyHexMeshDict", "system/controlDict",
                 "system/fvSchemes", "system/fvSolution", "constant/physicalProperties",
                 "constant/momentumTransport", "0/U", "0/p", "0/k", "0/epsilon", "0/nut"):
        assert (tmp_path / name).is_file()
        assert tmp_path / name in result.generated_paths
    manifest = json.loads((tmp_path / "constant/moosasCase.json").read_text())
    assert manifest["scenario"] == scenario
    assert manifest["conditions"] == conditions
    assert manifest["openfoam_version"] == 12


def test_indoor_rejects_unknown_openings_before_writing(indoor_model, tmp_path):
    conditions = indoor_conditions()
    conditions["inlet"]["opening"] = "missing"
    with pytest.raises(ValueError, match="opening"):
        exportFoam(indoor_model, tmp_path / "case.foam", scenario="indoor", space_index=39,
                   conditions=conditions)
    assert not list(tmp_path.iterdir())


def test_outdoor_requires_explicit_physical_conditions(building, tmp_path):
    with pytest.raises(ValueError, match="conditions"):
        exportFoam(building, tmp_path / "case.foam", scenario="outdoor")
    assert not list(tmp_path.iterdir())


def test_outdoor_rejects_point_inside_building(building, tmp_path):
    conditions = outdoor_conditions()
    conditions["inside_point"] = [-56, 180, 2]
    with pytest.raises(ValueError, match="inside_point"):
        exportFoam(building, tmp_path / "case.foam", scenario="outdoor", conditions=conditions)
    assert not list(tmp_path.iterdir())


def test_indoor_rejects_outward_inlet_velocity(indoor_model, tmp_path):
    conditions = indoor_conditions()
    conditions["inlet"]["velocity"] = [-0.409451, -0.286966, 0]
    with pytest.raises(ValueError, match="into the room"):
        exportFoam(indoor_model, tmp_path / "case.foam", scenario="indoor", space_index=39,
                   conditions=conditions)
    assert not list(tmp_path.iterdir())


def test_case_does_not_overwrite_existing_files(building, tmp_path):
    existing = tmp_path / "notes.txt"
    existing.write_text("keep")
    with pytest.raises(FileExistsError):
        exportFoam(building, tmp_path / "case.foam", scenario="outdoor", conditions=outdoor_conditions())
    assert existing.read_text() == "keep"
    assert not (tmp_path / "constant").exists()


def _run_case_with_openfoam(case_dir):
    """Exercise the exported files using OpenFOAM's own command-line tools."""
    logs = {}
    for command in (
        ("surfaceCheck", "constant/geometry/model.stl"),
        ("blockMesh",),
        ("snappyHexMesh", "-overwrite"),
        ("checkMesh", "-allTopology", "-meshQuality"),
        ("foamRun", "-solver", "incompressibleFluid"),
    ):
        result = subprocess.run(command, cwd=case_dir, capture_output=True,
                                text=True, timeout=180)
        logs[command[0]] = result.stdout + result.stderr
        assert result.returncode == 0, logs[command[0]]
    return logs


@pytest.mark.skipif(shutil.which("foamRun") is None, reason="OpenFOAM 12 is not on PATH")
@pytest.mark.parametrize("iterations", [1, 26])
def test_iteration_limit_is_not_reported_as_success(indoor_model, tmp_path, iterations):
    conditions = indoor_conditions()
    conditions["iterations"] = iterations
    exportFoam(indoor_model, tmp_path / "case.foam", scenario="indoor", space_index=39,
               grid_size=2, conditions=conditions)
    logs = _run_case_with_openfoam(tmp_path)
    assert "solution converged" not in logs["foamRun"].lower()
    assert (tmp_path / str(iterations) / "U").is_file()


@pytest.mark.skipif(shutil.which("foamRun") is None, reason="OpenFOAM 12 is not on PATH")
@pytest.mark.parametrize("scenario", ["indoor", "outdoor"])
def test_real_geo_solves_and_conserves_flow(building, indoor_model, tmp_path, scenario):
    model = indoor_model if scenario == "indoor" else building
    conditions = indoor_conditions() if scenario == "indoor" else outdoor_conditions()
    options = dict(space_index=39) if scenario == "indoor" else {}
    exportFoam(model, tmp_path / "case.foam", scenario=scenario, grid_size=2 if scenario == "indoor" else 4,
               conditions=conditions, **options)
    logs = _run_case_with_openfoam(tmp_path)
    assert "Surface is closed" in logs["surfaceCheck"]
    assert "Mesh OK" in logs["checkMesh"]
    assert "solution converged" in logs["foamRun"].lower()
    time_directories = [path for path in tmp_path.iterdir()
                        if path.is_dir() and path.name.replace(".", "", 1).isdigit()
                        and float(path.name) > 0]
    latest = max(time_directories, key=lambda path: float(path.name))
    for name in ("U", "p", "k", "epsilon"):
        assert (latest / name).is_file()
    manifest = json.loads((tmp_path / "constant/moosasCase.json").read_text())
    flows = []
    for patch in manifest["flow_patches"]:
        reports = list((tmp_path / "postProcessing" / f"flow_{patch}").glob("*/surfaceFieldValue.dat"))
        assert len(reports) == 1
        rows = [line.split() for line in reports[0].read_text().splitlines()
                if line.strip() and not line.lstrip().startswith("#")]
        assert float(rows[-1][0]) == float(latest.name)
        flows.append(float(rows[-1][1]))
    assert np.isfinite(flows).all()
    assert any(value < 0 for value in flows)
    assert any(value > 0 for value in flows)
    assert abs(sum(flows)) / -sum(value for value in flows if value < 0) < 0.01
