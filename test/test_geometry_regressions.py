from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from MoosasPy.simulation.radiation import calculation as radiation_calculation
from MoosasPy.model.io.graph import buildGraph
from MoosasPy.transform.geometry.convexify import GeometryConvexifier
from MoosasPy.model import MoosasModel
from MoosasPy.model import configure_model_resources
from MoosasPy.transform.geometry.element import (
    MoosasEdge,
    MoosasElement,
    MoosasFace,
    MoosasFloor,
    MoosasGlazing,
    MoosasGeometry,
    MoosasSpace,
    MoosasWall,
)
from MoosasPy.transform.geometry.geos import Projection, ccwNormal, is_ccw, simplify
from MoosasPy.transform.geometry.grid import MoosasGrid
from MoosasPy.transform.geometry.planar_graph import TopoNetwork
from MoosasPy.transform.geometry.planar_graph import TopoEdge
from MoosasPy.transform.geometry.polygon import GeometryOperator, GeometryValidator
from MoosasPy.transform.geometry.contour import _merge_tiny_partitions
from MoosasPy.transform.stages.convexification import convexify_model
from MoosasPy.transform.stages.classification import classify_model
from MoosasPy.transform.stages.cleansing import attach_explicit_shading, cleanse_model
from MoosasPy.transform.stages.splitting import split_wall_intersections
from MoosasPy.transform.stages.topology import _normalize_horizontal_faces, build_space_topology
from MoosasPy.utils import GeometryError, shapely
from MoosasPy.utils.constant import geom
from MoosasPy.transform.importers.geo import _readGeo


def test_geo_import_preserves_millimetre_details(tmp_path):
    source = tmp_path / "millimetre.geo"
    source.write_text(
        "f,0,millimetre\nfn,0,0,1\n"
        "fv,0.0034,0.0074,3.1234\n"
        "fv,1.0034,0.0074,3.1234\n"
        "fv,1.0034,1.0074,3.1234\n"
        "fv,0.0034,1.0074,3.1234\n;\n",
        encoding="utf-8",
    )

    geometry = _readGeo(str(source))

    assert geom.POINT_PRECISION == 0.001
    assert len(geometry) == 1
    coordinates = shapely.get_coordinates(geometry[0].face, include_z=True)[:-1]
    assert coordinates.min(axis=0) == pytest.approx([0.003, 0.007, 3.123])
    assert coordinates.max(axis=0) == pytest.approx([1.003, 1.007, 3.123])


def test_projection_round_trip_preserves_polygon_holes():
    polygon = shapely.Polygon(
        [(0, 0, 3), (4, 0, 3), (4, 4, 3), (0, 4, 3)],
        holes=[[(1, 1, 3), (2, 1, 3), (2, 2, 3), (1, 2, 3)]],
    )
    projection = Projection([.8, .8, 3], [0, 0, 1])
    restored = projection.toWorld(projection.toUV(polygon))
    assert len(restored.interiors) == 1
    assert restored.area == pytest.approx(15)
    np.testing.assert_allclose(
        shapely.get_coordinates(restored, include_z=True),
        shapely.get_coordinates(polygon, include_z=True), atol=1e-14,
    )


@pytest.mark.parametrize('explicit_holes', [False, True])
def test_triangulation_keeps_intrinsic_holes_without_duplicate_rings(explicit_holes):
    from MoosasPy.transform.geometry.triangulation import triangulate2dFace
    polygon = shapely.Polygon(
        [(0, 0), (4, 0), (4, 4), (0, 4)],
        holes=[[(1, 1), (2, 1), (2, 2), (1, 2)]],
    )
    parts, _ = triangulate2dFace(polygon, [polygon.interiors[0]] if explicit_holes else None)
    union = shapely.union_all(parts)
    assert union.symmetric_difference(polygon).area < 1e-8
    assert sum(part.area for part in parts) == pytest.approx(15)
    assert all(not part.interiors for part in parts)


def test_planar_repair_rejects_changed_area():
    from MoosasPy.transform.geometry.polygon import planar_surface
    bowtie = shapely.Polygon([(0, 0), (2, 2), (0, 2), (2, 0)])
    with pytest.raises(ValueError, match='changes area'):
        planar_surface(bowtie)
    narrow = shapely.Polygon([(0, 0), (1, 0), (1, 1e-10)])
    assert planar_surface(narrow).area == narrow.area


def test_radiance_keeps_tiny_physical_triangles():
    from MoosasPy.simulation.radiation.radiance import _mesh_to_radiance_object
    triangle = shapely.Polygon([(0, 0, 3), (1e-8, 0, 3), (0, 1e-8, 3)])
    text = _mesh_to_radiance_object([triangle], 'material', 'tiny')
    assert text.count(' polygon ') == 1
    assert '0 0 9' in text


def test_merged_duplicate_wall_area_counts_geometry_once():
    model = MoosasModel()
    model.levelList = [0.0, 3.0]
    line = shapely.LineString([(0, 0), (6, 0)])
    first = MoosasWall.fromProjection(line, 0, 3, model)
    second = MoosasWall.fromProjection(shapely.reverse(line), 0, 3, model)
    first.dissolve(second)

    assert len(np.array(first.face).flatten()) == 2
    assert first.area == pytest.approx(18)
    assert first.area3d(project=Projection(first.getWeightCenter(), first.normal)) == pytest.approx(18)


def test_overlapping_wall_faces_use_union_area():
    model = MoosasModel()
    model.levelList = [0.0, 3.0]
    first = MoosasWall.fromProjection(shapely.LineString([(0, 0), (4, 0)]), 0, 3, model)
    second = MoosasWall.fromProjection(shapely.LineString([(2, 0), (6, 0)]), 0, 3, model)
    merged = MoosasWall(model, [first.faceId, second.faceId])

    assert merged.area == pytest.approx(18)


@pytest.mark.parametrize("air_boundary", [False, True])
def test_wall_split_preserves_thermal_and_glazing_settings(air_boundary):
    model = MoosasModel()
    model.levelList = [0.0, 3.0]
    wall = MoosasWall.fromProjection(
        shapely.LineString([(0, 0), (8, 0)]), 0, 3, model, airBoundary=air_boundary
    )
    wall.settings["u_value"] = 0.35
    wall.explicit_settings.add("u_value")
    glazing = MoosasGlazing.fromProjection(shapely.LineString([(2, 0), (6, 0)]), 1, 2, model)
    glazing.U_Value = 1.4
    glazing.SHGC = 0.28
    glazing.operable = 0.4
    wall.add_glazing(glazing)
    model.wallList = [wall]
    model.glazingList = [glazing]

    parts = MoosasWall.break_(wall, shapely.Point(4, 0))

    assert len(parts) == 2
    assert sum(part.area for part in parts) == pytest.approx(wall.area)
    assert all(part.is_air_boundary == air_boundary for part in parts)
    assert all(part.settings["u_value"] == 0.35 for part in parts)
    assert all(part.explicit_settings == {"u_value"} for part in parts)
    windows = [window for part in parts for window in part.glazingElement]
    assert len(windows) == 2
    assert sum(window.area for window in windows) == pytest.approx(glazing.area)
    assert all(window.U_Value == 1.4 and window.SHGC == 0.28 and window.operable == 0.4 for window in windows)


@pytest.mark.parametrize("origin", [(0, 0), (1026.912, 84.523)])
@pytest.mark.parametrize("virtual_neighbor", [False, True])
def test_partial_shared_walls_are_split_and_linked_before_export(origin, virtual_neighbor):
    model = MoosasModel()
    configure_model_resources(model)
    model.levelList = [0.0, 3.0]
    for index, rectangle in enumerate([(0, 0, 4, 10), (4, 0, 8, 6)]):
        polygon = shapely.affinity.translate(shapely.box(*rectangle), *origin)
        coordinates = shapely.get_coordinates(polygon)
        walls = [
            MoosasWall.fromProjection(shapely.LineString([first, second]), 0, 3, model)
            for first, second in zip(coordinates[:-1], coordinates[1:])
        ]
        if index == 1 and virtual_neighbor:
            for wall in walls:
                if np.allclose(shapely.get_coordinates(wall.force_2d())[:, 0], origin[0] + 4):
                    wall.setCategory(2)
        floors = []
        for elevation in (0, 3):
            face_id = model.includeGeo(shapely.force_3d(polygon, z=elevation), cat=0)
            face = MoosasFace(model, face_id)
            model.faceList.append(face)
            floors.append(MoosasFloor([face]))
        space = MoosasSpace(floors[0], MoosasEdge(walls), floors[1], Uid=f"room-{index}")
        model.spaceList.append(space)
        model.wallList.extend(walls)

    build_space_topology(model, False)

    shared = [wall for wall in model.wallList if not wall.isOuter]
    assert len(shared) == 1
    assert shared[0].area == pytest.approx(18)
    assert not shared[0].is_air_boundary
    assert set(shared[0].space) == {"room-0", "room-1"}
    assert all(shared[0] in space.edge.wall for space in model.spaceList)
    assert sum(wall.area for wall in model.wallList if wall.isOuter) == pytest.approx(108)
    assert sum(space.area for space in model.spaceList) == pytest.approx(64)
    assert model.spaceList[0].neighbor.keys() == {"room-1"}
    wall_count = len(model.wallList)
    build_space_topology(model, False)
    assert len(model.wallList) == wall_count
    assert sum(wall.area for wall in model.wallList if wall.isOuter) == pytest.approx(108)


@pytest.mark.parametrize("origin", [(0.001, 0.001), (1113.982, 155.463)])
@pytest.mark.parametrize("transpose", [False, True])
@pytest.mark.parametrize("crossing", [False, True], ids=["tee", "cross"])
def test_wall_intersections_preserve_millimetre_junctions(origin, transpose, crossing):
    coordinates = np.array([
        (0.0, 0.0), (0.0, 8.008),
        (-2.002 if crossing else 0.0, 4.002), (6.001, 4.002),
    ]) + origin
    if transpose:
        coordinates = coordinates[:, ::-1]
    start, end, branch_start, branch_end = coordinates
    junction = np.array([start[0], branch_start[1]]) if not transpose else np.array([branch_start[0], start[1]])
    model = MoosasModel()
    model.levelList = [0.0, 3.0]
    model.wallList = [
        MoosasWall.fromProjection(shapely.LineString([first, second]), 0.0, 3.0, model)
        for first, second in [(start, end), (branch_start, branch_end)]
    ]

    split_wall_intersections(model, enabled=True)

    expected = [(start, junction), (junction, end)]
    expected += [(branch_start, junction), (junction, branch_end)] if crossing else [(branch_start, branch_end)]
    projections = [wall.force_2d() for wall in model.wallList]
    assert len(projections) == len(expected)
    for first, second in expected:
        segment = shapely.set_precision(shapely.LineString([first, second]), geom.POINT_PRECISION)
        assert any(shapely.equals(segment, projection) for projection in projections)
    junction = shapely.set_precision(shapely.Point(junction), geom.POINT_PRECISION)
    assert all(
        any(shapely.equals(endpoint, junction) for endpoint in shapely.points(shapely.get_coordinates(projection)))
        for projection in projections
    )


@pytest.mark.parametrize("offset", [(0, 0), (10000, 10000)])
@pytest.mark.parametrize("point,orientation", [
    ((1, 0.1), 1),
    ((1, -0.1), -1),
    ((1, 0), 0),
    ((3, 0), 0),
    ((1, 0.00001), 1),
    ((1, -0.00001), -1),
])
def test_polygon_orientation_is_translation_independent(offset, point, orientation):
    a, b, c = np.array([(0, 0), (2, 0), point], dtype=float) + offset

    assert GeometryValidator._is_collinear(a, b, c) == (orientation == 0)
    assert GeometryValidator._is_left_on(a, b, c) == (orientation >= 0)
    assert GeometryValidator._is_left_on(b, a, c) == (orientation <= 0)


@pytest.mark.parametrize("offset", [(0, 0), (10000, 10000)])
@pytest.mark.parametrize("points,expected", [
    (((0, 0), (2, 0), (1, 0)), True),
    (((0, 0), (2, 0), (2, 0)), True),
    (((0, 0), (2, 0), (1, 0.1)), False),
    (((0, 0), (2, 0), (3, 0)), False),
    (((0, 0), (0, 2), (0, 1)), True),
    (((0, 0), (0, 0), (0, 0)), True),
    (((0, 0), (0, 0), (1, 0)), False),
])
def test_polygon_point_on_segment(points, expected, offset):
    a, b, c = np.asarray(points, dtype=float) + offset

    assert GeometryValidator._is_between(a, b, c) == expected
    assert GeometryValidator._is_between(b, a, c) == expected


@pytest.mark.parametrize("offset", [(0, 0), (10000, 10000)])
@pytest.mark.parametrize("points,expected", [
    (((0, 0), (2, 2), (0, 2), (2, 0)), True),
    (((0, 0), (2, 0), (2, 0), (3, 1)), True),
    (((0, 0), (2, 0), (1, 0), (1, 1)), True),
    (((0, 0), (2, 0), (-1, 0), (1, 0)), True),
    (((0, 0), (2, 0), (-1, 0), (3, 0)), True),
    (((0, 0), (2, 0), (3, 0), (4, 0)), False),
    (((0, 0), (2, 0), (0, 0.1), (2, 0.1)), False),
    (((0, 0), (2, 0), (0, 0.00001), (2, 0.00001)), False),
    (((0, 0), (2, 0), (3, -1), (3, 1)), False),
    (((0, 0), (0, 0), (-1, 0), (1, 0)), True),
    (((0, 0), (0, 0), (1, 0), (2, 0)), False),
    (((0, 0), (0, 0), (0, 0), (0, 0)), True),
    (((0, 0), (0, 0), (1, 0), (1, 0)), False),
])
def test_polygon_segment_intersection_is_symmetric(points, expected, offset):
    a, b, c, d = np.asarray(points, dtype=float) + offset
    for first, second in [((a, b), (c, d)), ((c, d), (a, b))]:
        for segment1 in (first, first[::-1]):
            for segment2 in (second, second[::-1]):
                assert GeometryValidator._is_intersect(*segment1, *segment2) == expected


@pytest.mark.parametrize("offset", [(0, 0), (10000, 10000)])
@pytest.mark.parametrize("points,pair,expected", [
    ([(0, 0), (2, 0), (2, 2), (0, 2)], (0, 2), True),
    ([(0, 0), (2, 0), (2, 2), (0, 2)], (0, 1), False),
    ([(0, 0), (2, 0), (2, 2), (0, 2)], (0, 0), False),
    ([(0, 0), (1, 0), (2, 0), (2, 2), (0, 2)], (0, 2), False),
    ([(0, 0), (1, 0), (2, 0), (2, 2), (0, 2)], (1, 3), True),
    ([(0, 0), (3, 0), (3, 1), (1, 1), (1, 3), (0, 3)], (2, 4), False),
    ([(0, 0), (3, 0), (3, 1), (1, 1), (1, 3), (0, 3)], (0, 3), True),
    ([(0, 0), (3, 0), (3, 1), (1, 1), (1, 3), (0, 3)], (1, 5), False),
    ([(0, 0), (4, 0), (4, 4), (2, 2), (0, 4)], (0, 2), False),
])
def test_polygon_diagonal_is_strictly_internal(points, pair, expected, offset):
    vertices = np.column_stack((np.asarray(points, dtype=float) + offset, np.zeros(len(points))))
    # The current subpolygon need not use consecutive indices into the source.
    source = np.repeat(vertices, 2, axis=0)
    indices = list(range(0, len(source), 2))

    assert GeometryValidator._is_diagonal(source, indices, *pair) == expected
    assert GeometryValidator._is_diagonal(source, indices, *pair[::-1]) == expected


@pytest.mark.parametrize("offset", [(0, 0), (10000, 10000)])
@pytest.mark.parametrize("points", [
    [
        (0, 0), (8, 0), (8, 4), (7, 4), (7, 2), (6, 2),
        (6, 4), (5, 4), (5, 2), (4, 2), (4, 4), (0, 4),
    ],
    [(0, 0), (8, 0), (8, 4), (4, 3.9999), (0, 4)],
])
def test_convex_partition_is_translation_independent(offset, points):
    points = np.asarray(points, dtype=float)

    def partition(coordinates):
        vertices = np.column_stack((coordinates, np.zeros(len(coordinates))))
        _, _, _, faces, _ = GeometryConvexifier.convexify_faces(
            [0], ["comb"], [np.array([0, 0, 1])], [vertices], [[]],
        )
        return [shapely.Polygon(face[:, :2]) for face in faces]

    reference = partition(points)
    translated = partition(points + offset)
    union = shapely.union_all(translated)
    original = shapely.Polygon(points + offset)

    assert len(translated) == len(reference)
    assert len(translated) > 1
    assert all(part.is_valid and part.area > 0 for part in translated)
    assert all(part.convex_hull.area - part.area < 1e-8 for part in translated)
    assert union.symmetric_difference(original).area < 1e-8
    assert sum(part.area for part in translated) - union.area == pytest.approx(0, abs=1e-8)
    assert sorted(part.area for part in translated) == pytest.approx(sorted(part.area for part in reference))


@pytest.mark.parametrize("offset", [(0, 0, 0), (10000, 10000, 3)])
def test_hole_bridges_do_not_cross_polygon_or_holes(offset):
    outer = np.array([(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0)]) + offset
    holes = {
        0: np.array([(2, 2, 0), (2, 4, 0), (4, 4, 0), (4, 2, 0)]) + offset,
        1: np.array([(6, 6, 0), (6, 8, 0), (8, 8, 0), (8, 6, 0)]) + offset,
    }
    merged, bridges = GeometryOperator.merge_holes(outer, holes)
    polygon = shapely.Polygon(outer[:, :2])

    assert len(bridges) == 2
    assert shapely.Polygon(merged[:, :2]).area == pytest.approx(92)
    for bridge in bridges:
        line = shapely.LineString(bridge[:, :2])
        assert polygon.covers(line)
        assert all(line.intersection(shapely.Polygon(hole[:, :2])).length == 0 for hole in holes.values())


@pytest.mark.parametrize("axis_z,axis_x", [
    ((0, 0, 1), (1, 0, 0)),
    ((0, 0, -1), (1, 0, 0)),
    ((0, 0, 1), (-1, 0, 0)),
    ((0, 0, -1), (-1, 0, 0)),
    ((1, 0, 0), (0, 1, 0)),
    ((-1, 0, 0), (0, 1, 0)),
    ((0, 1, 0), (1, 0, 0)),
    ((0, -1, 0), (1, 0, 0)),
    ((0, -0.6, 0.8), (1, 0, 0)),
])
def test_projection_is_right_handed_and_round_trips(axis_z, axis_x):
    projection = Projection((12.0, -7.0, 3.0), axis_z, axis_x)
    local = shapely.Point(2.0, 4.0, 1.5)
    expected = (
        projection.origin.array
        + 2.0 * np.array(axis_x)
        + 4.0 * np.cross(axis_z, axis_x)
        + 1.5 * np.array(axis_z)
    )

    assert np.linalg.det(projection.rotateMatrix) == pytest.approx(1.0)
    world = projection.toWorld(local)
    assert world.coords[0] == pytest.approx(expected)
    assert projection.toUV(world).coords[0] == pytest.approx(local.coords[0])


@pytest.mark.parametrize("reverse", [False, True])
def test_ccw_check_handles_collinear_vertices(reverse):
    points = [(0, 0), (1, 0), (2, 0), (3, 0), (4, 0), (4, 1),
              (4, 2), (3, 2), (2, 2), (1, 2), (0, 2), (0, 1)]
    if reverse:
        points.reverse()
    polygon = shapely.Polygon(points)

    assert is_ccw(polygon) == (not reverse)
    assert is_ccw(polygon.exterior) == (not reverse)
    assert is_ccw(shapely.LineString(polygon.exterior.coords)) == (not reverse)
    edge = object.__new__(MoosasEdge)
    edge._MoosasEdge__botBound = polygon
    assert edge.is_ccw() == (not reverse)


@pytest.mark.parametrize("points", [
    [(0, 0), (2, 0), (0, 1)],
    [(0, 0), (2, 0), (2, 1), (0, 1)],
    [(0, 0), (3, 0), (3, 1), (1, 1), (1, 3), (0, 3)],
    [(0, 0), (1, 0), (2, 0), (3, 0), (3, 2), (0, 2)],
])
@pytest.mark.parametrize("axis_x,axis_y", [
    ((1, 0, 0), (0, 1, 0)),
    ((1, 0, 0), (0, -1, 0)),
    ((1, 0, 0), (0, 0, 1)),
    ((0, 1, 0), (0, 0, 1)),
    ((1, 0, 0), (0, 0.8, 0.6)),
])
@pytest.mark.parametrize("reverse", [False, True])
def test_ccw_normal_follows_vertex_order(points, axis_x, axis_y, reverse):
    vertices = np.array([
        np.array([1000.0, 200.0, 3.0]) + x * np.array(axis_x) + y * np.array(axis_y)
        for x, y in points
    ])
    expected = np.cross(axis_x, axis_y)
    if reverse:
        vertices = vertices[::-1]
        expected = -expected

    for start in range(len(vertices)):
        polygon = shapely.Polygon(np.roll(vertices, start, axis=0))
        assert ccwNormal(polygon).array == pytest.approx(expected)


def test_simplify_keeps_a_narrow_valid_triangle_closed():
    """A shallow-angle triangle must not be collapsed into a two-point ring."""
    triangle = shapely.polygons(np.array([
        [-7.292645493490398, -0.1346168870321888, 0.0],
        [0.5721716279931042, 0.3190918803725927, 0.0],
        [14.013119358987678, -0.049858106308219, 0.0],
        [-7.292645493490398, -0.1346168870321888, 0.0],
    ]))

    result = simplify(triangle, include_z=True)
    coordinates = shapely.get_coordinates(result, include_z=True)

    assert len(coordinates) == 4
    assert np.allclose(coordinates[0], coordinates[-1])
    assert shapely.is_valid(result)
    assert shapely.area(result) > 1.0


def test_simplify_keeps_three_vertices_for_a_2d_triangle():
    triangle = shapely.polygons(np.array([
        [0.0, 0.0],
        [10.0, 0.1],
        [20.0, 0.0],
        [0.0, 0.0],
    ]))

    result = simplify(triangle)

    assert shapely.is_valid(result)
    assert len(shapely.get_coordinates(result)) == 4


def test_attach_shading_controls_explicit_shading_faces():
    face = shapely.polygons(np.array([
        [0.0, 0.0, 0.0],
        [1.0, 0.0, 0.0],
        [1.0, 1.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0],
    ]))

    def prepare(attach_shading):
        model = MoosasModel()
        model.geometryList = [
            MoosasGeometry(face, "floor", shapely.points([0.0, 0.0, 1.0]), 4),
            MoosasGeometry(face, "shade", shapely.points([0.0, 0.0, 1.0]), -1),
        ]
        model.geoId = [geometry.faceId for geometry in model.geometryList]
        model = classify_model(model, attach_shading=attach_shading)
        assert model.shadingList == []
        model, _ = cleanse_model(
            model,
            solve_duplicated=False,
            solve_redundant=False,
            solve_overlap=False,
        )
        if attach_shading:
            model = attach_explicit_shading(model)
        return model

    assert prepare(False).shadingList == []
    assert [element.faceId for element in prepare(True).shadingList] == ["shade"]


def test_single_face_element_preserves_directed_normal():
    model = MoosasModel()
    geometry = MoosasGeometry(
        shapely.polygons(np.array([
            [0.0, 0.0, 0.0],
            [0.0, 0.0, 1.0],
            [1.0, 0.0, 1.0],
            [1.0, 0.0, 0.0],
            [0.0, 0.0, 0.0],
        ])),
        "south",
        shapely.points([0.0, -1.0, 0.0]),
    )
    model.geometryList = [geometry]
    model.geoId = [geometry.faceId]
    element = MoosasElement(model, geometry)

    assert np.allclose(element.normal, [0.0, -1.0, 0.0])
    geometry.flip = True
    assert np.allclose(element.normal, [0.0, 1.0, 0.0])


def test_horizontal_faces_preserve_close_structural_levels():
    model = MoosasModel()
    lower = shapely.polygons(np.array([
        [0.0, 0.0, 0.0],
        [2.0, 0.0, 0.0],
        [2.0, 2.0, 0.0],
        [0.0, 2.0, 0.0],
        [0.0, 0.0, 0.0],
    ]))
    upper = shapely.force_3d(shapely.force_2d(lower), z=1.219)
    geometries = [
        MoosasGeometry(lower, "lower", shapely.points([0.0, 0.0, 1.0]), 4),
        MoosasGeometry(upper, "upper", shapely.points([0.0, 0.0, 1.0]), 4),
    ]
    model.geometryList = geometries
    model.geoId = [geometry.faceId for geometry in geometries]

    MoosasFace(model, geometries[0])
    upper_face = MoosasFace(model, geometries[1])

    assert model.levelList == [0.0, 1.219]
    assert upper_face.level == pytest.approx(1.219)
    assert upper_face.offset == pytest.approx(0.0)


def test_shared_horizontal_faces_are_collapsed_and_ordered_by_storey():
    polygon = shapely.polygons(np.array([
        [0.0, 0.0, 1.0],
        [2.0, 0.0, 1.0],
        [2.0, 2.0, 1.0],
        [0.0, 2.0, 1.0],
        [0.0, 0.0, 1.0],
    ]))
    lower_face = SimpleNamespace(
        normal=np.array([0.0, 0.0, 1.0]),
        level=1.0,
        offset=0.0,
        space=["old-lower"],
        isOuter=True,
        parentFloors=[],
        force_2d=lambda: shapely.force_2d(polygon),
    )
    upper_face = SimpleNamespace(
        normal=np.array([0.0, 0.0, -1.0]),
        level=1.0,
        offset=0.0,
        space=["old-upper"],
        isOuter=True,
        parentFloors=[],
        force_2d=lambda: shapely.force_2d(polygon),
    )
    lower_floor = SimpleNamespace(level=0.0, offset=0.0, face=[])
    lower_ceiling = SimpleNamespace(face=[lower_face])
    upper_floor = SimpleNamespace(level=1.0, offset=0.0, face=[upper_face])
    upper_ceiling = SimpleNamespace(face=[])
    lower = SimpleNamespace(
        id="lower",
        floor=SimpleNamespace(level=0.0, offset=0.0, face=[]),
        ceiling=lower_ceiling,
    )
    upper = SimpleNamespace(
        id="upper",
        floor=upper_floor,
        ceiling=SimpleNamespace(level=2.0, offset=0.0, face=[]),
    )
    model = SimpleNamespace(
        spaceList=[lower, upper],
        faceList=[lower_face, upper_face],
    )

    _normalize_horizontal_faces(model)

    assert model.faceList == [lower_face]
    assert lower_ceiling.face == [lower_face]
    assert upper_floor.face == [lower_face]
    assert lower_face.space == ["lower", "upper"]
    assert lower_face.isOuter is False


def test_topology_node_keys_normalize_signed_zero():
    edge = SimpleNamespace(
        force_2d=lambda: shapely.linestrings([[-0.0, 0.0], [1.0, 0.0]]),
        Uid="edge",
    )

    topology_edge = TopoEdge(0, edge)

    assert topology_edge.fromPStr == "0.0_0.0"


def test_low_enclosed_space_is_void():
    floor = SimpleNamespace(level=0.0, offset=0.0, area=4.0)
    ceiling = SimpleNamespace(level=1.219, offset=0.0, area=4.0)
    edge = SimpleNamespace(area=4.0)
    space = object.__new__(MoosasSpace)
    space.floor = floor
    space.ceiling = ceiling
    space.edge = edge
    space.space_type = "room"

    assert space.is_void()


def test_graph_serializes_explicit_shading_semantics():
    face = shapely.polygons(np.array([
        [0.0, 0.0, 1.0],
        [1.0, 0.0, 1.0],
        [1.0, 1.0, 1.0],
        [0.0, 1.0, 1.0],
        [0.0, 0.0, 1.0],
    ]))
    model = MoosasModel()
    geometry = MoosasGeometry(face, "shade", shapely.points([0.0, 0.0, 1.0]), -1)
    model.geometryList = [geometry]
    model.geoId = [geometry.faceId]
    shading = MoosasElement(model, geometry)
    model.shadingList = [shading]

    graph = buildGraph(model)

    assert shading.Uid in graph.graph
    assert graph.graph.nodes[shading.Uid]["face_params"]["t"] == "shading"


def test_projection_supports_sloped_roof_without_horizontal_section_vertices():
    roof = shapely.polygons(np.array([
        [0.0, 0.0, 3.0],
        [6.0, 0.0, 3.0],
        [6.0, 4.0, 6.0],
        [0.0, 4.0, 6.0],
        [0.0, 0.0, 3.0],
    ]))

    projection = Projection.fromPolygon(roof)
    projected_roof = projection.toUV(roof)

    assert shapely.area(projected_roof) > 0.0
    assert np.isfinite(projection.rotateMatrix).all()


def test_sloped_roof_radiation_uses_offset_world_grid_points(monkeypatch):
    roof = shapely.polygons(np.array([
        [0.0, 0.0, 3.0],
        [6.0, 0.0, 3.0],
        [6.0, 4.0, 6.0],
        [0.0, 4.0, 6.0],
        [0.0, 0.0, 3.0],
    ]))
    model = MoosasModel()
    model.levelList = [0.0, 3.0, 6.0]
    geometry = MoosasGeometry(roof, "sloped-roof")
    model.geometryList = [geometry]
    model.geoId = [geometry.faceId]
    face = MoosasFace(model, geometry)
    grid = MoosasGrid(face, 1.0, 0.2)
    automatic_grid = MoosasGrid(face, grid_offset=0.2)
    world_points = [point.coords[0] for point in grid.gridPoints]
    projected_points = [grid.proj.toUV(point).coords[0] for point in grid.gridPoints]
    traced_rays = []

    def capture_rays(rays, **_):
        traced_rays.extend(rays)
        return [None] * len(rays)

    monkeypatch.setattr(radiation_calculation, "ray_test", capture_rays)
    result = radiation_calculation.calculate_face_radiation(
        face,
        grid_size=1.0,
        grid_offset=0.2,
        reflection=0,
        geo_path="unused.geo",
    )

    assert world_points
    assert automatic_grid.gridSize == pytest.approx(1.2)
    assert all(point[2] == pytest.approx(0.2) for point in projected_points)
    assert np.allclose(traced_rays[0].origin.array, world_points[0])
    assert result.shape == (145,)


def test_convexification_does_not_infer_vertical_air_walls():
    """Zone air walls are created from final contours, not stacked face diagonals."""
    footprint = np.array([
        [0.0, 0.0],
        [8.0, 0.0],
        [8.0, 3.0],
        [3.0, 3.0],
        [3.0, 8.0],
        [0.0, 8.0],
    ])
    faces = [
        np.column_stack((footprint, np.full(len(footprint), elevation)))
        for elevation in (0.0, 3.0)
    ]

    categories, _, _, _, divide_lines = GeometryConvexifier.convexify_faces(
        [0, 0],
        ["floor", "roof"],
        [np.array([0.0, 0.0, 1.0])] * 2,
        faces,
        [None, None],
    )

    assert divide_lines
    assert all(int(category) != 2 for category in categories)


def test_convexified_model_preserves_the_next_geometry_id():
    model = MoosasModel()
    model.levelList = [0.0, 3.0]
    geometry = MoosasGeometry(
        np.array([
            [0.0, 0.0, 0.0],
            [0.0, 0.0, 3.0],
            [5.0, 0.0, 3.0],
            [5.0, 0.0, 0.0],
        ]),
        "n40",
        np.array([0.0, -1.0, 0.0]),
        0,
    )
    model.geometryList = [geometry]
    model.geoId = [geometry.faceId]
    model.wallList = [MoosasWall(model, geometry)]
    model.newIndex = 41

    result = convexify_model(model)
    generated_id = result.includeGeo(
        shapely.polygons([
            [0.0, 1.0, 0.0],
            [0.0, 1.0, 3.0],
            [5.0, 1.0, 3.0],
            [5.0, 1.0, 0.0],
        ]),
        cat=2,
    )

    assert generated_id == "n41"
    assert len(result.geoId) == len(set(result.geoId))


def test_tiny_partition_is_merged_into_its_neighbor():
    tiny = shapely.box(0.0, 0.0, 0.01, 5.0)
    room = shapely.box(0.01, 0.0, 10.0, 5.0)

    result = _merge_tiny_partitions([tiny, room])

    assert len(result) == 1
    assert shapely.area(result[0]) == pytest.approx(50.0)


def test_negative_45_degree_wall_projects_to_a_line():
    model = MoosasModel()
    model.levelList = [0.0, 3.0]
    geometry = MoosasGeometry(
        np.array([
            [0.0, 5.0, 0.0],
            [0.0, 5.0, 3.0],
            [5.0, 0.0, 3.0],
            [5.0, 0.0, 0.0],
        ]),
        "negative_45_wall",
        np.array([1.0, 1.0, 0.0]),
        0,
    )
    model.geometryList = [geometry]
    model.geoId = [geometry.faceId]

    projection = MoosasWall(model, geometry).force_2d()

    assert shapely.get_type_id(projection) == shapely.GeometryType.LINESTRING
    assert shapely.length(projection) == pytest.approx(np.sqrt(50.0))


def test_rotated_rectangle_merges_half_grid_corner_rounding():
    model = MoosasModel()
    model.levelList = [0.0, 3.0]
    p0 = np.array([3.93, -8.98])
    p1 = np.array([9.0, -3.92])
    p2 = np.array([-3.87, 8.95])
    p3_from_first_wall = np.array([-8.934999999999999, 3.8850000000000007])
    p3_from_second_wall = np.array([-8.935000000000002, 3.8849999999999962])
    segments = (
        (p0, p1),
        (p1, p2),
        (p2, p3_from_first_wall),
        (p3_from_second_wall, p0),
    )

    walls = []
    for index, (start, end) in enumerate(segments):
        direction = end - start
        normal = np.array([direction[1], -direction[0], 0.0])
        normal /= np.linalg.norm(normal)
        geometry = MoosasGeometry(
            np.array([
                [start[0], start[1], 0.0],
                [start[0], start[1], 3.0],
                [end[0], end[1], 3.0],
                [end[0], end[1], 0.0],
            ]),
            f"rotated_wall_{index}",
            normal,
            0,
        )
        model.geometryList.append(geometry)
        model.geoId.append(geometry.faceId)
        walls.append(MoosasWall(model, geometry))
    model.wallList = np.array(walls)

    network = TopoNetwork.inLevel(0.0, model)

    assert len(network.edges) == 4
    assert len(network.nodes) == 4
    assert len(network.outerBoundary()) == 1


def test_select_wall_does_not_create_missing_air_boundaries():
    model = MoosasModel()
    model.levelList = [0.0, 3.0]
    geometry = MoosasGeometry(
        np.array([
            [0.0, 0.0, 0.0],
            [0.0, 0.0, 3.0],
            [5.0, 0.0, 3.0],
            [5.0, 0.0, 0.0],
        ]),
        "only_existing_wall",
        np.array([0.0, -1.0, 0.0]),
        0,
    )
    model.geometryList = [geometry]
    model.geoId = [geometry.faceId]
    wall = MoosasWall(model, geometry)
    model.wallList = np.array([wall])
    wall_count = len(model.wallList)
    boundary = shapely.polygons([[0.0, 0.0], [5.0, 0.0], [5.0, 5.0], [0.0, 0.0]])

    with pytest.raises(GeometryError, match="no existing wall matches boundary segment"):
        MoosasEdge.selectWall(boundary, model.wallList)

    assert len(model.wallList) == wall_count


def test_template_application_rejects_flat_legacy_templates():
    template = {
        "type": "RESIDENTIAL",
        "zone_ppsm": "0.031",
        "zone_equipment": "4.2",
        "zone_lighting": "6.5",
    }
    model = SimpleNamespace(
        buildingTemplate={"test-residential": template},
    )
    space = object.__new__(MoosasSpace)
    space.edge = SimpleNamespace(parent=model)
    space.settings = {}

    with pytest.raises(ValueError, match="namespaced id"):
        space.applySettings(template)
    with pytest.raises(ValueError, match="version 5"):
        space.applySettings("test-residential")
