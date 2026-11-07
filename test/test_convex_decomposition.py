from types import SimpleNamespace

import numpy as np
import pytest
import shapely
from shapely.affinity import rotate, translate

from MoosasPy.transform.geometry.contour import _merge_tiny_partitions
from MoosasPy.transform.geometry.cleanse import splitFaces
from MoosasPy.transform.geometry.convexify import GeometryConvexifier, convexify_shape
from MoosasPy.transform.geometry.element import MoosasFace, MoosasFloor, MoosasSkylight
from MoosasPy.transform.geometry.polygon import GeometryOperator, cut_hole
from MoosasPy.model import MoosasModel
from MoosasPy.transform.stages.assembly import cap_floor_simple
from MoosasPy.utils import GeometryError, TopologyError
from MoosasPy.utils.constant import geom


NOTCHED_FOOTPRINT = [
    (0, 0), (12, 0), (12, 8), (8, 8),
    (8, 7.7), (6, 7.7), (6, 8), (0, 8),
]


def assert_partition(parts, source):
    union = shapely.union_all(parts)
    assert union.symmetric_difference(source).area == pytest.approx(0, abs=1e-8)
    assert sum(part.area for part in parts) - union.area == pytest.approx(0, abs=1e-8)
    for part in parts:
        assert part.is_valid
        assert not part.interiors
        assert part.area > 0
        assert part.convex_hull.area - part.area == pytest.approx(0, abs=1e-8)


@pytest.mark.parametrize("points", [
    NOTCHED_FOOTPRINT,
    [(0, 0), (10, 0), (10, 6), (9, 6), (9, 5.8), (7, 5.8), (7, 6), (0, 6)],
])
def test_shortest_diagonal_split_preserves_notched_footprints(points):
    vertices = np.column_stack((points, np.zeros(len(points))))
    before = vertices.copy()

    indices, _ = GeometryOperator.split_poly(vertices, list(range(len(vertices))))
    parts = [shapely.Polygon(vertices[part, :2]) for part in indices]

    assert_partition(parts, shapely.Polygon(points))
    assert all(shapely.is_ccw(part.exterior) for part in parts)
    np.testing.assert_array_equal(vertices, before)


@pytest.mark.parametrize("angle", [0, 1.2])
@pytest.mark.parametrize("scale,offset", [
    (1.0, (0, 0)), (1.0, (10000, -25000)), (0.01, (17, 29)), (100.0, (-10, 15)),
])
def test_shortest_diagonal_split_preserves_geometry_under_transforms(angle, scale, offset):
    points = np.asarray(NOTCHED_FOOTPRINT, dtype=float)
    rotation = np.array([[np.cos(angle), -np.sin(angle)],
                         [np.sin(angle), np.cos(angle)]])
    transformed = points @ rotation.T * scale + offset
    vertices = np.column_stack((transformed, np.full(len(points), 3)))
    indices, _ = GeometryOperator.split_poly(vertices, list(range(len(vertices))))
    parts = [shapely.Polygon(vertices[part, :2]) for part in indices]
    assert_partition(parts, shapely.Polygon(transformed))
    assert sum(part.area for part in parts) / scale ** 2 == pytest.approx(
        shapely.Polygon(points).area, rel=1e-7,
    )


def test_first_reflex_vertex_uses_its_shortest_valid_diagonal():
    vertices = np.array([(0, 0), (8, 0), (8, 3), (3, 3), (3, 8), (0, 8)])
    faces, diagonals = GeometryOperator.split_poly(vertices, list(range(len(vertices))))
    assert diagonals == [[3, 0]]
    assert len(faces) == 2


def test_split_preserves_source_indices():
    points = np.asarray(NOTCHED_FOOTPRINT, dtype=float)
    vertices = np.repeat(np.column_stack((points, np.zeros(len(points)))), 2, axis=0)
    source_indices = list(range(0, len(vertices), 2))

    faces, diagonals = GeometryOperator.split_poly(vertices, np.asarray(source_indices))
    assert all(index in source_indices for face in faces for index in face)
    polygons = [shapely.Polygon(vertices[face, :2]) for face in faces]
    assert_partition(polygons, shapely.Polygon(points))
    assert len(diagonals) == len(faces) - 1


def test_convex_input_keeps_original_index_order():
    vertices = np.array([(0, 0), (4, 0), (4, 3), (0, 3)], dtype=float)
    faces, diagonals = GeometryOperator.split_poly(vertices, [0, 1, 2, 3])
    assert faces == [[0, 1, 2, 3]]
    assert diagonals == []


def test_subminimum_zone_is_absorbed_even_when_the_union_is_concave():
    parts = [shapely.box(0, 0, 4, 1), shapely.box(0, 1, 0.1, 2)]
    result = _merge_tiny_partitions(parts)
    assert len(result) == 1
    assert result[0].is_valid
    assert not result[0].interiors
    assert result[0].equals(shapely.union_all(parts))
    assert result[0].convex_hull.area > result[0].area


@pytest.mark.parametrize("width", [1, 1.1])
def test_zone_at_or_above_minimum_area_is_not_forced_into_concave_neighbor(width):
    parts = [shapely.box(0, 0, 4, 1), shapely.box(0, 1, width, 2)]
    result = _merge_tiny_partitions(parts)
    assert len(result) == 2
    assert_partition(result, shapely.union_all(parts))


def test_large_adjacent_convex_zones_are_not_merged():
    parts = [shapely.box(0, 0, 4, 3), shapely.box(4, 0, 8, 3)]
    result = _merge_tiny_partitions(parts)
    assert len(result) == 2
    assert_partition(result, shapely.box(0, 0, 8, 3))


@pytest.mark.parametrize("angle", [0, 37])
@pytest.mark.parametrize("triangular", [False, True])
def test_slender_zone_above_minimum_area_is_absorbed(angle, triangular):
    room = shapely.box(0, -2, 60, 0)
    slender = (
        shapely.Polygon([(0, 0), (60, 0), (30, 0.05)])
        if triangular else shapely.box(0, 0, 60, 0.05)
    )
    parts = [
        translate(rotate(part, angle, origin=(0, 0)), xoff=1940, yoff=52)
        for part in [room, slender]
    ]
    expected = shapely.union_all(parts)

    result = _merge_tiny_partitions(parts)

    assert parts[1].area > geom.ROOM_MIN_AREA
    assert len(result) == 1
    assert result[0].is_valid
    assert not result[0].interiors
    assert result[0].equals(expected)
    assert result[0].area == pytest.approx(sum(part.area for part in parts))


@pytest.mark.parametrize("width,expected_count", [
    (0.099, 1), (0.1, 2), (0.101, 2),
])
def test_slender_zone_width_threshold_preserves_wider_long_zones(width, expected_count):
    parts = [shapely.box(0, -2, 30, 0), shapely.box(0, 0, 30, width)]

    result = _merge_tiny_partitions(parts)

    assert len(result) == expected_count
    assert_partition(result, shapely.union_all(parts))


@pytest.mark.parametrize("length,expected_count", [
    (0.999, 2), (1.0, 2), (1.001, 1),
])
def test_slender_zone_aspect_ratio_threshold(monkeypatch, length, expected_count):
    monkeypatch.setattr(geom, "ROOM_MIN_AREA", 0)
    parts = [shapely.box(0, -2, length, 0), shapely.box(0, 0, length, 0.05)]

    result = _merge_tiny_partitions(parts)

    assert len(result) == expected_count
    assert_partition(result, shapely.union_all(parts))


def test_slender_zone_chooses_the_longest_shared_edge():
    room = shapely.box(0, -2, 30, 0)
    slender = shapely.box(0, 0, 30, 0.05)
    other = shapely.box(30, 0, 32, 2)

    result = _merge_tiny_partitions([room, slender, other])

    assert len(result) == 2
    assert any(part.equals(room.union(slender)) for part in result)
    assert any(part.equals(other) for part in result)
    assert_partition(result, shapely.union_all([room, slender, other]))


def test_isolated_slender_zone_is_preserved_with_warning(capsys):
    slender = shapely.box(0, 0, 30, 0.05)
    room = shapely.box(0, 1, 30, 3)

    result = _merge_tiny_partitions([slender, room])

    assert len(result) == 2
    assert any(part.equals(slender) for part in result)
    assert "no eligible edge neighbor" in capsys.readouterr().out


def test_subminimum_zone_chooses_the_longest_shared_edge():
    first = shapely.box(0, 0, 4, 2)
    tiny = shapely.box(1, 2, 2, 2.5)
    second = shapely.box(2, 2, 4, 4)
    result = _merge_tiny_partitions([first, tiny, second])
    assert len(result) == 2
    merged = next(part for part in result if part.covers(tiny))
    assert merged.covers(first)
    assert not merged.covers(second)
    assert shapely.union_all(result).equals(shapely.union_all([first, tiny, second]))


def test_isolated_small_zone_does_not_block_other_small_zone_merges(capsys):
    isolated = shapely.box(-5, -5, -4.9, -4.9)
    room = shapely.box(0, 0, 4, 1)
    attached = shapely.box(0, 1, 0.1, 2)
    result = _merge_tiny_partitions([isolated, room, attached])
    assert len(result) == 2
    assert any(part.equals(room.union(attached)) for part in result)
    assert any(part.equals(isolated) for part in result)
    assert "no eligible edge neighbor" in capsys.readouterr().out


def test_subminimum_zone_merging_preserves_courtyard_holes():
    parts = [
        shapely.box(0, 0, 3, 0.3), shapely.box(0, 2.7, 3, 3),
        shapely.box(0, 0.3, 0.3, 2.7), shapely.box(2.7, 0.3, 3, 2.7),
    ]
    expected = shapely.union_all(parts)
    result = _merge_tiny_partitions(parts)
    assert shapely.union_all(result).equals(expected)
    assert all(part.is_valid and not part.interiors for part in result)
    assert sum(part.area for part in result) == pytest.approx(expected.area)


def test_holed_decomposition_uses_double_cuts_without_filling_holes():
    exterior = np.array([(0, 0, 3), (12, 0, 3), (12, 10, 3), (0, 10, 3)])
    hole = np.array([(4, 3, 3), (8, 3, 3), (8, 7, 3), (4, 7, 3)])
    faces, _ = GeometryConvexifier.convexify_faces_2d([exterior], [[hole]])
    parts = [shapely.Polygon(face[:, :2]) for face in faces]
    assert_partition(parts, shapely.Polygon(exterior[:, :2], [hole[:, :2]]))
    assert all(np.all(face[:, 2] == 3) for face in faces)


@pytest.mark.parametrize("offset,scale", [((0, 0), 1), ((10000, -25000), 1), ((17, 29), 0.01)])
@pytest.mark.parametrize("reverse", [False, True])
def test_double_cuts_preserve_multiple_unequal_holes(offset, scale, reverse):
    exterior = np.array([(0, 0), (20, 0), (20, 16), (0, 16)], dtype=float)
    holes = [
        np.array([(2, 2), (6, 2), (6, 5), (2, 5)], dtype=float),
        np.array([(10, 7), (15, 7), (16, 10), (13, 13), (10, 11)], dtype=float),
    ]
    rings = [ring * scale + offset for ring in [exterior, *holes]]
    rings = [np.column_stack((ring, np.full(len(ring), 3))) for ring in rings]
    if reverse:
        rings = [ring[::-1] for ring in rings]
    before = [ring.copy() for ring in rings]

    faces, _ = GeometryConvexifier.convexify_faces_2d([rings[0]], [rings[1:]])

    assert_partition(
        [shapely.Polygon(face[:, :2]) for face in faces],
        shapely.Polygon(rings[0][:, :2], [ring[:, :2] for ring in rings[1:]]),
    )
    assert all(np.all(face[:, 2] == 3) for face in faces)
    for actual, expected in zip(rings, before):
        np.testing.assert_array_equal(actual, expected)


@pytest.mark.parametrize("footprint", [
    [(0, 0), (50, 0), (50, 18), (40, 18), (40, 8), (30, 8),
     (30, 18), (20, 18), (20, 8), (10, 8), (10, 18), (0, 18)],
    [(0, 0), (35, 0), (35, 35), (25, 35), (25, 8), (10, 8),
     (10, 35), (0, 35)],
])
def test_offset_band_with_disconnected_cores_uses_simple_double_cut_regions(footprint):
    source = shapely.Polygon(footprint)
    core = source.buffer(-4.572, join_style="mitre")
    assert len(shapely.get_parts(core)) > 1
    band = source.difference(core)
    assert band.is_valid
    assert len(band.interiors) > 1

    pending = [band]
    simple = []
    while pending:
        piece = pending.pop()
        if piece.interiors:
            cells = cut_hole(piece)
            assert sum(len(cell.interiors) for cell in cells) == len(piece.interiors) - 1
            pending.extend(cells)
        else:
            simple.append(piece)
    assert all(piece.is_valid and piece.exterior.is_simple for piece in simple)

    faces, _ = convexify_shape(
        shapely.Polygon(band.exterior), [shapely.Polygon(ring) for ring in band.interiors]
    )
    assert_partition(faces, band)


def test_double_cuts_are_shared_by_2d_and_model_face_entry_points():
    exterior = np.array([(0, 0, 3), (20, 0, 3), (20, 16, 3), (0, 16, 3)])
    holes = [
        np.array([(2, 2, 3), (6, 2, 3), (6, 5, 3), (2, 5, 3)]),
        np.array([(10, 7, 3), (15, 7, 3), (16, 10, 3), (13, 13, 3), (10, 11, 3)]),
    ]
    faces_2d, _ = GeometryConvexifier.convexify_faces_2d([exterior.copy()], [holes])
    categories, face_ids, normals, faces_3d, _ = GeometryConvexifier.convexify_faces(
        [0], ["floor"], [np.array([0, 0, 1])], [exterior.copy()], [holes],
    )
    expected = shapely.Polygon(exterior[:, :2], [hole[:, :2] for hole in holes])
    assert_partition([shapely.Polygon(face[:, :2]) for face in faces_3d], expected)
    assert len(faces_2d) == len(faces_3d) == len(set(face_ids))
    assert categories == [0] * len(faces_3d)
    assert all(np.array_equal(normal, [0, 0, 1]) for normal in normals)


def test_double_cuts_report_failed_simple_polygon_split(monkeypatch):
    monkeypatch.setattr(GeometryOperator, "split_poly", lambda vertices, indices: ([[0, 1, 2]], []))
    exterior = np.array([(0, 0, 0), (12, 0, 0), (12, 10, 0), (0, 10, 0)])
    hole = np.array([(4, 3, 0), (8, 3, 0), (8, 7, 0), (4, 7, 0)])
    with pytest.raises(GeometryError, match="lost area|overlapping"):
        GeometryConvexifier.convexify_faces_2d([exterior], [[hole]])


def test_double_cuts_report_missing_connector_pair(monkeypatch):
    monkeypatch.setattr(shapely, "polygonize", lambda lines: shapely.GeometryCollection())
    source = shapely.Polygon(
        [(0, 0), (12, 0), (12, 10), (0, 10)],
        [[(4, 3), (8, 3), (8, 7), (4, 7)]],
    )
    with pytest.raises(GeometryError, match="No valid pair"):
        cut_hole(source)


def test_horizontal_face_split_keeps_small_remaining_physical_fragments():
    model = MoosasModel()
    geometry_id = model.includeGeo(shapely.force_3d(shapely.box(0, 0, 10, 10)), cat=0)
    face = MoosasFace(model, geometry_id)
    model.faceList = [face]
    edge = SimpleNamespace(force_2d=lambda: shapely.box(0.05, 0, 5, 10))

    inside, outside = splitFaces(face, edge)
    parts = [item.force_2d() for item in [inside] + outside]
    assert_partition(parts, shapely.box(0, 0, 10, 10))
    assert min(part.area for part in parts) == pytest.approx(0.5)


@pytest.mark.parametrize("bounds,inside_areas,outside_areas", [
    ((0.005, 2, 0.045, 3), [0.04], []),
    ((5, 5, 5.5, 5.5), [], [0.25]),
    ((0.05, 2, 0.55, 3), [], [0.5]),
    ((0.025, 2, 0.075, 3), [0.025], [0.025]),
    ((0.025, 2, 2.075, 3), [0.025], [2.025]),
])
def test_small_horizontal_split_preserves_skylight_geometry_and_parent(
    bounds, inside_areas, outside_areas,
):
    model = MoosasModel()
    face = MoosasFace(
        model, model.includeGeo(shapely.force_3d(shapely.box(0, 0, 10, 10)), cat=0),
    )
    skylight = MoosasSkylight(
        model, model.includeGeo(shapely.force_3d(shapely.box(*bounds)), cat=1),
    )
    model.faceList = [face]
    model.skylightList = [skylight]
    face.add_glazing(skylight)
    edge = SimpleNamespace(force_2d=lambda: shapely.box(0, 0, 0.05, 10))

    inside, outside = splitFaces(face, edge)

    assert [part.area for part in inside.glazingElement] == pytest.approx(inside_areas)
    assert sorted(part.area for parent in outside for part in parent.glazingElement) == (
        pytest.approx(outside_areas)
    )
    attached = [part for parent in [inside] + outside for part in parent.glazingElement]
    assert set(attached) == set(model.skylightList)
    for part in attached:
        assert part.parentFace in [inside] + outside
        assert part.force_2d().difference(part.parentFace.force_2d()).area == (
            pytest.approx(0, abs=1e-8)
        )
        assert part.category == skylight.category
    geometries = [part.force_2d() for part in attached]
    assert_partition(geometries, skylight.force_2d())
    if len(attached) == 1:
        assert attached[0] is skylight
    else:
        assert skylight not in model.skylightList


def test_horizontal_split_reports_a_skylight_without_a_covering_parent():
    model = MoosasModel()
    face = MoosasFace(
        model, model.includeGeo(shapely.force_3d(shapely.box(0, 0, 10, 10)), cat=0),
    )
    skylight = MoosasSkylight(
        model, model.includeGeo(shapely.force_3d(shapely.box(20, 20, 20.5, 20.5)), cat=1),
    )
    model.faceList = [face]
    model.skylightList = [skylight]
    face.add_glazing(skylight)
    edge = SimpleNamespace(force_2d=lambda: shapely.box(0, 0, 0.05, 10))

    with pytest.raises(TopologyError, match="no covering parent face"):
        splitFaces(face, edge)


@pytest.mark.parametrize("gap,expected_openings", [(0.0005, 0), (0.1, 1)])
def test_floor_cap_distinguishes_coordinate_roundoff_from_real_openings(gap, expected_openings):
    model = MoosasModel()
    geometry_id = model.includeGeo(
        shapely.force_3d(shapely.box(0, 0, 10 - gap, 10)), cat=0,
    )
    face = MoosasFace(model, geometry_id)
    model.faceList = [face]
    floor = MoosasFloor([face])

    capped = cap_floor_simple(shapely.box(0, 0, 10, 10), 0, model, floor)
    assert len(model.skylightList) == expected_openings
    assert len(capped.face) == 1 + expected_openings


def test_horizontal_split_rejects_a_snapped_copy_of_its_parent():
    model = MoosasModel()
    polygon = shapely.Polygon([(0, 0), (10, 0), (10, 5), (5, 5.0004), (0, 5)])
    geometry_id = model.includeGeo(shapely.force_3d(polygon), cat=0)
    face = MoosasFace(model, geometry_id)
    model.faceList = [face]
    edge = SimpleNamespace(force_2d=lambda: shapely.box(0, 0, 10, 5))
    before = len(model.geometryList)

    assert splitFaces(face, edge) is None
    assert model.faceList == [face]
    assert len(model.geometryList) == before


def test_courtyard_floor_cap_preserves_a_real_opening_inside_the_room():
    courtyard = shapely.box(3, 3, 7, 7)
    boundary = shapely.box(0, 0, 10, 10).difference(courtyard)
    opening = shapely.box(1, 1, 2, 2)
    model = MoosasModel()
    face = MoosasFace(
        model, model.includeGeo(shapely.force_3d(boundary.difference(opening)), cat=0),
    )
    model.faceList = [face]

    capped = cap_floor_simple(boundary, 0, model, MoosasFloor([face]))

    assert len(model.skylightList) == 1
    aperture = model.skylightList[0]
    assert aperture.force_2d().equals(opening)
    assert aperture.force_2d().intersection(courtyard).area == 0
    assert shapely.union_all([part.force_2d() for part in capped.face]).equals(boundary)
