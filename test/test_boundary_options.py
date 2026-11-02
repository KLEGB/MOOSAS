from __future__ import annotations

import pytest

from MoosasPy.model import MoosasModel
from MoosasPy.transform import TransformOptions
from MoosasPy.transform.stages.boundary import prepare_boundary_geometry
from MoosasPy.transform.geometry.element import MoosasGeometry, MoosasWall
from MoosasPy.transform.stages.classification import classify_model
from MoosasPy.transform.stages.validation import validate_model
from MoosasPy.utils import np, shapely


def _three_story_box() -> MoosasModel:
    model = MoosasModel()
    faces = []
    for index, z_value in enumerate((0.0, 3.0, 6.0, 9.0)):
        faces.append(
            MoosasGeometry(
                np.array([
                    [0.0, 0.0, z_value],
                    [10.0, 0.0, z_value],
                    [10.0, 8.0, z_value],
                    [0.0, 8.0, z_value],
                ]),
                f'level_{index}',
                np.array([0.0, 0.0, 1.0]),
            )
        )
    walls = (
        ([[0, 0, 0], [0, 0, 9], [10, 0, 9], [10, 0, 0]], [0, -1, 0]),
        ([[10, 0, 0], [10, 0, 9], [10, 8, 9], [10, 8, 0]], [1, 0, 0]),
        ([[10, 8, 0], [10, 8, 9], [0, 8, 9], [0, 8, 0]], [0, 1, 0]),
        ([[0, 8, 0], [0, 8, 9], [0, 0, 9], [0, 0, 0]], [-1, 0, 0]),
    )
    for index, (face, normal) in enumerate(walls):
        faces.append(MoosasGeometry(np.asarray(face), f'wall_{index}', np.asarray(normal)))
    model.geometryList = faces
    model.geoId = [geometry.faceId for geometry in faces]
    model.newIndex = len(faces)
    return model


def test_boundary_options_default_to_disabled():
    options = TransformOptions()
    assert options.simplify_boundary is False
    assert options.insert_core is False


def test_air_boundary_projection_creates_only_a_wall():
    model = MoosasModel()
    model.levelList = [0.0, 3.0]

    wall = MoosasWall.fromProjection(
        shapely.linestrings([[0.0, 0.0], [5.0, 0.0]]),
        bottom=0.0,
        top=3.0,
        model=model,
        airBoundary=True,
    )

    assert wall.is_air_boundary
    assert wall.glazingElement == []
    assert len(model.glazingList) == 0


def test_air_boundary_without_model_spaces_is_valid():
    model = MoosasModel()
    model.levelList = [0.0, 3.0]
    wall = MoosasWall.fromProjection(
        shapely.linestrings([[0.0, 0.0], [5.0, 0.0]]),
        bottom=0.0,
        top=3.0,
        model=model,
        airBoundary=True,
    )
    model.wallList = [wall]

    assert validate_model(model) is model

    wall.space = ["missing-space"]
    with pytest.raises(ValueError, match="references an unknown model space"):
        validate_model(model)


def test_category_two_vertical_geometry_is_classified_as_air_boundary_wall():
    model = _three_story_box()
    for geometry in model.geometryList[:4]:
        geometry.setCategory(4)
    air_geometry = MoosasGeometry(
        np.array([
            [2.0, 0.0, 0.0],
            [2.0, 0.0, 3.0],
            [2.0, 8.0, 3.0],
            [2.0, 8.0, 0.0],
        ]),
        "air_boundary",
        np.array([1.0, 0.0, 0.0]),
        2,
    )
    model.geometryList.append(air_geometry)
    model.geoId.append(air_geometry.faceId)

    classified = classify_model(model, triangulate_faces=False, break_wall_vertical=False)
    air_walls = [wall for wall in classified.wallList if wall.is_air_boundary]

    assert len(air_walls) == 1
    assert air_walls[0].glazingElement == []
    assert all(glazing.category != 2 for glazing in classified.glazingList)


def test_disabled_boundary_operations_preserve_geometry_objects():
    model = _three_story_box()
    original_geometry = model.geometryList

    result = prepare_boundary_geometry(
        model,
        simplify_boundary=False,
    )

    assert result is model
    assert result.geometryList is original_geometry


def test_simplify_boundary_builds_one_obb_per_story():
    model = prepare_boundary_geometry(
        _three_story_box(),
        simplify_boundary=True,
    )

    assert len(model.geometryList) == 18
    assert {
        geometry.faceId.split('_')[0]
        for geometry in model.geometryList
        if geometry.faceId.startswith('layer')
    } == {'layer1', 'layer2', 'layer3'}


def test_simplify_boundary_rejects_single_level_geometry():
    model = _three_story_box()
    model.geometryList = model.geometryList[:1]

    with pytest.raises(ValueError, match='at least two horizontal levels'):
        prepare_boundary_geometry(
            model,
            simplify_boundary=True,
        )
