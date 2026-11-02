"""Read the exported files independently and check finite-volume invariants."""

import json
import re
import shutil
import subprocess
from io import StringIO
from pathlib import Path

import numpy as np
import pytest
import shapely

from MoosasPy.model import MoosasModel
from MoosasPy.model.io import foam
from MoosasPy.model.io.foam import exportFoam
from MoosasPy.transform.geometry.element import MoosasElement, MoosasGeometry
from MoosasPy.transform.geometry.grid import MoosasGrid
from MoosasPy.transform import transform


@pytest.fixture(scope="module")
def geo_model():
    source = Path(__file__).parent / "caseFile" / "test0_6spacesIntersection.geo"
    return transform(str(source), input_type="geo", stdout=StringIO())


@pytest.mark.parametrize("index,area,base", [
    (0, 32.400552, 0.0), (1, 87.168652, 0.0),
    (2, 32.400552, 4.4), (3, 87.168652, 4.4),
    (4, 32.400552, 8.8), (5, 87.168652, 8.8),
])
def test_save_real_geo_room_through_model(geo_model, tmp_path, index, area, base):
    target = tmp_path / "room" / "room.foam"
    result = exportFoam(geo_model, target, space_index=index, grid_size=1.0, layers=8)
    assert len(geo_model.spaceList) == 6
    assert result.primary_path == target
    assert target.read_bytes() == b""
    assert len(result.generated_paths) == 7
    assert all(path.is_file() for path in result.generated_paths)
    points, _, _ = check_volume_mesh(target.parent, area * 4.4)
    assert points[:, 2].min() == pytest.approx(base)
    assert points[:, 2].max() == pytest.approx(base + 4.4)


def test_transformed_spaces_have_stable_plan_order(geo_model):
    spaces = geo_model.spaceList
    keys = [
        (
            space.level,
            float(shapely.centroid(space.force_2d()).y),
            float(shapely.centroid(space.force_2d()).x),
            str(space.id),
        )
        for space in spaces
    ]
    assert keys == sorted(keys)


def test_export_real_geo_through_function(tmp_path):
    source = Path(__file__).parent / "caseFile" / "test3_geomove.geo"
    model = transform(str(source), input_type="geo", stdout=StringIO())
    result = exportFoam(model, str(tmp_path / "room.foam"), space_index=0, layers=4)
    assert result.primary_path == tmp_path / "room.foam"
    check_volume_mesh(tmp_path, 33.0760407 * 9.26)


@pytest.mark.parametrize("entry_point", ["model", "dispatcher"])
def test_generic_save_rejects_openfoam(geo_model, tmp_path, entry_point):
    from MoosasPy.model.io import save_model

    target = tmp_path / "room.foam"
    with pytest.raises(ValueError, match="exportFoam"):
        if entry_point == "model":
            geo_model.save(target, space_index=0)
        else:
            save_model(geo_model, target, space_index=0)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("size", [0, -1, float("nan"), True])
def test_save_rejects_invalid_grid_size(geo_model, tmp_path, size):
    with pytest.raises(ValueError, match="grid_size"):
        exportFoam(geo_model, tmp_path / "room.foam", space_index=0, grid_size=size)
    assert not list(tmp_path.iterdir())


def test_save_does_not_overwrite_existing_marker(geo_model, tmp_path):
    target = tmp_path / "room.foam"
    target.write_text("existing")
    with pytest.raises(FileExistsError):
        exportFoam(geo_model, target, space_index=0)
    assert target.read_text() == "existing"
    assert not (tmp_path / "constant").exists()


def test_save_openfoam_requires_explicit_room(geo_model, tmp_path):
    with pytest.raises(TypeError, match="space_index"):
        exportFoam(geo_model, tmp_path / "room.foam")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("index", [-1, 6, True, 0.5])
def test_save_openfoam_rejects_invalid_room(geo_model, tmp_path, index):
    with pytest.raises(ValueError, match="space_index"):
        exportFoam(geo_model, tmp_path / "room.foam", space_index=index)
    assert not list(tmp_path.iterdir())


def test_save_rejects_options_for_other_formats(geo_model, tmp_path):
    with pytest.raises(TypeError, match="options"):
        geo_model.save(tmp_path / "building.idf", space_index=0)
    assert not list(tmp_path.iterdir())


def make_grid(coordinates=None, size=1.0, offset=0.78, holes=None):
    if coordinates is None:
        coordinates = [(0, 0, 0), (2, 0, 0), (2, 2, 0), (0, 2, 0)]
    model = MoosasModel()
    geometry = MoosasGeometry(shapely.Polygon(coordinates), "floor", holes=holes)
    model.geometryList.append(geometry)
    model.geoId.append(geometry.faceId)
    element = MoosasElement(model, geometry, level=0, offset=0)
    return MoosasGrid(element, gird_size=size, grid_offset=offset)


def read_list(path):
    document = path.read_text(encoding="ascii")
    document = re.sub(r"//[^\n]*", "", document)
    body = document.split("}", 1)[1].strip()
    match = re.fullmatch(r"(\d+)\s*\(\s*(.*?)\s*\)\s*", body, re.S)
    assert match, f"Invalid OpenFOAM list: {path}"
    return int(match[1]), match[2]


def read_mesh(case):
    directory = case / "constant" / "polyMesh"
    count, body = read_list(directory / "points")
    points = np.array([[float(x) for x in row.split()]
                       for row in re.findall(r"\(([^()]*)\)", body)])
    assert points.shape == (count, 3)
    count, body = read_list(directory / "faces")
    faces = []
    for length, row in re.findall(r"(\d+)\(([^()]*)\)", body):
        face = [int(x) for x in row.split()]
        assert len(face) == int(length)
        faces.append(face)
    assert len(faces) == count
    labels = []
    for name in ("owner", "neighbour"):
        count, body = read_list(directory / name)
        data = np.array([int(x) for x in body.split()], dtype=int)
        assert len(data) == count
        labels.append(data)
    count, body = read_list(directory / "boundary")
    patches = {}
    for name, fields in re.findall(r"(\w+)\s*\{([^}]*)\}", body):
        patches[name] = dict(re.findall(r"(\w+)\s+(\w+)\s*;", fields))
    assert len(patches) == count
    return points, faces, *labels, patches


def check_volume_mesh(case, expected_volume, expected_wall_area=None):
    points, faces, owner, neighbour, patches = read_mesh(case)
    assert len(owner) == len(faces)
    assert np.all(owner[:len(neighbour)] < neighbour)
    cell_count = int(owner.max()) + 1
    assert set(owner) | set(neighbour) == set(range(cell_count))
    closure = np.zeros((cell_count, 3))
    volumes = np.zeros(cell_count)
    reference = points.mean(axis=0)
    edge_uses = [dict() for _ in range(cell_count)]
    face_areas = []
    for index, face in enumerate(faces):
        assert len(face) == len(set(face)) >= 3
        vertices = points[face] - reference
        area = sum((np.cross(vertices[j] - vertices[0], vertices[j + 1] - vertices[0])
                    for j in range(1, len(vertices) - 1)), start=np.zeros(3)) / 2
        assert np.linalg.norm(area) > 0
        face_areas.append(np.linalg.norm(area))
        volume = np.dot(vertices[0], area) / 3
        incidence = [(owner[index], 1)]
        if index < len(neighbour):
            incidence.append((neighbour[index], -1))
        for cell, sign in incidence:
            closure[cell] += sign * area
            volumes[cell] += sign * volume
            for a, b in zip(face, face[1:] + face[:1]):
                edge = tuple(sorted((a, b)))
                edge_uses[cell].setdefault(edge, []).append(sign * (1 if a < b else -1))
    np.testing.assert_allclose(closure, 0, atol=1e-8)
    assert np.all(volumes > 0)
    assert volumes.sum() == pytest.approx(expected_volume, rel=1e-8, abs=1e-10)
    assert all(len(uses) == 2 and sum(uses) == 0
               for edges in edge_uses for uses in edges.values())
    start = len(neighbour)
    for patch in patches.values():
        assert int(patch["startFace"]) == start
        start += int(patch["nFaces"])
    assert start == len(faces)
    if expected_wall_area is not None:
        start = int(patches["walls"]["startFace"])
        end = start + int(patches["walls"]["nFaces"])
        assert sum(face_areas[start:end]) == pytest.approx(expected_wall_area, rel=1e-8)
    return points, volumes, patches


def test_export_covers_whole_floor_and_preserves_grid_mapping(tmp_path):
    grid = make_grid()
    original_mask = grid.mask
    original_points = grid.gridPoints.copy()
    result = foam._write_grid_mesh(grid, tmp_path, height=3, layers=3)

    points, volumes, patches = check_volume_mesh(tmp_path, 12, expected_wall_area=24)
    # The 2x2 floor needs 3x3 columns including half-width perimeter cells.
    assert len(volumes) == 27
    np.testing.assert_allclose(points.min(axis=0), (0, 0, 0))
    np.testing.assert_allclose(points.max(axis=0), (2, 2, 3))
    assert set(patches) == {"bottom", "top", "walls"}
    assert all(patch["type"] == "wall" for patch in patches.values())
    mapping = json.loads((tmp_path / "constant" / "moosasGrid.json").read_text())
    assert mapping["grid_shape"] == [2, 2]
    assert len(mapping["cells"]) == 27
    assert [cell["cell"] for cell in mapping["cells"]] == list(range(27))
    aligned = [cell for cell in mapping["cells"] if cell["sample_index"] == 0]
    assert [cell["layer"] for cell in aligned] == [0, 1, 2]
    assert all(cell["grid_index"] == [1, 1] for cell in aligned)
    np.testing.assert_allclose(mapping["sample_points"],
                               shapely.get_coordinates(original_points, include_z=True))
    assert grid.mask == original_mask
    np.testing.assert_array_equal(grid.gridPoints, original_points)
    assert result.primary_path == tmp_path / "constant" / "polyMesh"
    assert len(result.generated_paths) == 6
    assert all(path.is_file() for path in result.generated_paths)


def test_sloping_projection_and_explicit_base_offset(tmp_path):
    grid = make_grid([(1, 0, 0), (1, 0, 2), (1, 2, 2), (1, 2, 0)])
    foam._write_grid_mesh(grid, tmp_path, height=2, layers=2, base_offset=0.25)
    points, _, _ = check_volume_mesh(tmp_path, 8)
    # The legacy projection chooses -X; sampling offset must not move the mesh.
    np.testing.assert_allclose(points.min(axis=0), (-1.25, 0, 0), atol=1e-8)
    np.testing.assert_allclose(points.max(axis=0), (0.75, 2, 2), atol=1e-8)


@pytest.mark.parametrize("footprint", [
    shapely.Polygon([(0, 0), (2, 0), (2, 0.7), (0.7, 0.7), (0.7, 2), (0, 2)]),
    shapely.Polygon([(0, 0), (2, 0), (2, 2), (0, 2)],
                    holes=[[(0.8, 0.8), (1.2, 0.8), (1.2, 1.2), (0.8, 1.2)]]),
    shapely.Polygon([(0, 0), (2, 0), (1.4, 1.9)]),
])
def test_clipped_concave_and_holed_cells_are_conformal(tmp_path, footprint):
    grid = make_grid(
        [(x, y, 0) for x, y in footprint.exterior.coords],
        holes=[shapely.force_3d(shapely.Polygon(ring)) for ring in footprint.interiors],
    )
    foam._write_grid_mesh(grid, tmp_path, height=1.5, layers=2)
    check_volume_mesh(tmp_path, footprint.area * 1.5, expected_wall_area=footprint.length * 1.5)


@pytest.mark.parametrize("options", [
    {"height": 0}, {"height": -1}, {"height": float("nan")},
    {"height": float("inf")}, {"height": 1, "layers": 0},
    {"height": 1, "layers": 1.5}, {"height": 1, "layers": True},
    {"height": 1, "base_offset": float("nan")},
    {"height": 1, "patch_types": {"top": "wall; injected"}},
    {"height": 1, "patch_types": {"unknown": "wall"}},
])
def test_invalid_options_do_not_write_files(tmp_path, options):
    with pytest.raises((ValueError, TypeError)):
        foam._write_grid_mesh(make_grid(), tmp_path / "case", **options)
    assert not (tmp_path / "case").exists()


def test_boundary_types_and_repeatable_output(tmp_path):
    grid = make_grid(size=3)  # No valid samples, but a nonempty volume domain.
    options = {"height": 1, "patch_types": {"top": "patch", "bottom": "patch"}}
    foam._write_grid_mesh(grid, tmp_path / "first", **options)
    foam._write_grid_mesh(grid, tmp_path / "second", **options)
    _, _, patches = check_volume_mesh(tmp_path / "first", 4)
    assert patches["top"]["type"] == "patch"
    for path in (tmp_path / "first").rglob("*"):
        if path.is_file():
            assert path.read_bytes() == (tmp_path / "second" / path.relative_to(tmp_path / "first")).read_bytes()


def test_existing_mesh_is_not_silently_overwritten(tmp_path):
    grid = make_grid()
    foam._write_grid_mesh(grid, tmp_path, height=1)
    before = (tmp_path / "constant" / "polyMesh" / "points").read_bytes()
    with pytest.raises(FileExistsError):
        foam._write_grid_mesh(grid, tmp_path, height=2)
    assert (tmp_path / "constant" / "polyMesh" / "points").read_bytes() == before


def test_inclined_face_keeps_world_geometry_and_volume(tmp_path):
    grid = make_grid([(4, 5, 6), (6, 5, 6), (6, 6.6, 7.2), (4, 6.6, 7.2)])
    foam._write_grid_mesh(grid, tmp_path, height=2, layers=4)
    points, _, _ = check_volume_mesh(tmp_path, 8, expected_wall_area=16)
    source = np.array([(4, 5, 6), (6, 5, 6), (6, 6.6, 7.2), (4, 6.6, 7.2)])
    # Source corners remain on the base, with a congruent top 2m along local Z.
    for corner in source:
        assert np.min(np.linalg.norm(points - corner, axis=1)) < 1e-8
        expected_top = corner + 2 * grid.proj.axisZ
        assert np.min(np.linalg.norm(points - expected_top, axis=1)) < 1e-8


def test_grid_holes_exclude_sampling_points_and_remain_unmodified(tmp_path):
    hole = shapely.Polygon([(0.8, 0.8, 0), (1.2, 0.8, 0), (1.2, 1.2, 0), (0.8, 1.2, 0)])
    grid = make_grid(holes=[hole], size=0.5)
    before = grid.UVFace.wkb
    assert grid.UVFace.area == pytest.approx(3.84)
    for point in grid.gridPoints:
        assert not hole.contains(point)
    foam._write_grid_mesh(grid, tmp_path, height=1)
    check_volume_mesh(tmp_path, 3.84, expected_wall_area=9.6)
    assert grid.UVFace.wkb == before


@pytest.mark.parametrize("attribute,value", [
    ("gridSize", 0), ("gridSize", float("nan")),
    ("gridCell", np.empty((0, 0), dtype=object)),
    ("UVFace", shapely.Polygon()),
    ("UVFace", shapely.Polygon([(0, 0), (1, 1), (0, 1), (1, 0)])),
])
def test_invalid_grid_is_rejected_before_writing(tmp_path, attribute, value):
    grid = make_grid()
    setattr(grid, attribute, value)
    with pytest.raises(ValueError):
        foam._write_grid_mesh(grid, tmp_path / "case", height=1)
    assert not (tmp_path / "case").exists()


def test_stale_mesh_sidecars_are_not_mixed_with_new_mesh(tmp_path):
    mesh_dir = tmp_path / "constant" / "polyMesh"
    mesh_dir.mkdir(parents=True)
    (mesh_dir / "cellZones").write_text("old zones")
    with pytest.raises(FileExistsError):
        foam._write_grid_mesh(make_grid(), tmp_path, height=1)
    assert not (mesh_dir / "points").exists()


@pytest.mark.skipif(shutil.which("checkMesh") is None, reason="OpenFOAM checkMesh is not installed")
def test_openfoam_checkmesh_accepts_real_geo(geo_model, tmp_path):
    exportFoam(geo_model, tmp_path / "room.foam", space_index=0, grid_size=1.0, layers=8)
    write_checkmesh_dictionaries(tmp_path)
    result = subprocess.run(
        [shutil.which("checkMesh"), "-case", str(tmp_path), "-allTopology", "-allGeometry"],
        capture_output=True, text=True, timeout=60,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "Mesh OK" in output, output


@pytest.mark.skipif(shutil.which("checkMesh") is None, reason="OpenFOAM checkMesh is not installed")
@pytest.mark.parametrize("with_hole", [False, True])
def test_openfoam_checkmesh_accepts_export(tmp_path, with_hole):
    hole = shapely.Polygon([(0.8, 0.8, 0), (1.2, 0.8, 0), (1.2, 1.2, 0), (0.8, 1.2, 0)])
    grid = make_grid(holes=[hole] if with_hole else None)
    foam._write_grid_mesh(grid, tmp_path, height=2, layers=2)
    write_checkmesh_dictionaries(tmp_path)
    result = subprocess.run(
        [shutil.which("checkMesh"), "-case", str(tmp_path), "-allTopology", "-allGeometry"],
        capture_output=True, text=True, timeout=60,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "Mesh OK" in output, output


def write_checkmesh_dictionaries(case_dir):
    system = case_dir / "system"
    system.mkdir()
    header = 'FoamFile { version 2.0; format ascii; class dictionary; object %s; }\n'
    (system / "controlDict").write_text(
        header % "controlDict" + "application checkMesh; startFrom startTime; startTime 0; "
        "stopAt endTime; endTime 1; deltaT 1; writeControl timeStep; writeInterval 1;\n"
    )
    (system / "fvSchemes").write_text(
        header % "fvSchemes" + "ddtSchemes { default steadyState; } "
        "gradSchemes { default Gauss linear; } divSchemes { default none; } "
        "laplacianSchemes { default Gauss linear corrected; } "
        "interpolationSchemes { default linear; } snGradSchemes { default corrected; }\n"
    )
    (system / "fvSolution").write_text(header % "fvSolution" + "solvers {}\n")
