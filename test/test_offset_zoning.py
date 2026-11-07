from io import StringIO
from pathlib import Path

import numpy as np
import pytest
import shapely
from eppy.modeleditor import IDF
from shapely.affinity import rotate, translate

from MoosasPy.model.io.idf import exportIDF
from MoosasPy.model.io.idf.version import configure_idd
from MoosasPy.transform import TransformOptions, transform
from MoosasPy.transform.geometry.contour import _merge_tiny_partitions
from MoosasPy.transform.geometry.core import (
    _collapse_narrow_core,
    offset_partitions,
    validate_partition_lines,
)
from MoosasPy.transform.geometry.polygon import _validate_partition
from MoosasPy.utils import GeometryError
from MoosasPy.utils.constant import geom


def test_default_offset_and_internal_configuration(monkeypatch):
    source = shapely.box(0, 0, 30, 20)
    perimeter, core, cuts = offset_partitions(source)
    assert shapely.union_all(core).equals(shapely.box(5, 5, 25, 15))
    assert not cuts
    _validate_partition(perimeter + core, source, convex=True)
    monkeypatch.setattr(geom, "CORE_OFFSET_DEPTH", 3)
    _, core, _ = offset_partitions(source)
    assert shapely.union_all(core).equals(shapely.box(3, 3, 27, 17))
    assert "core_offset_depth" not in TransformOptions.__dataclass_fields__


@pytest.mark.parametrize("width", [1.99, 2.0, 2.01])
@pytest.mark.parametrize("angle", [0, 37])
def test_narrow_core_retains_original_length_and_two_sided_partition(width, angle):
    core = rotate(shapely.box(0, 0, 20, width), angle)
    source = core.buffer(5, join_style="mitre")
    wide, cuts = _collapse_narrow_core(source, core, 2)
    if width < 2:
        assert wide.is_empty
        assert len(cuts) == 1
        assert cuts[0].length == pytest.approx(20, abs=1e-6)
        assert len(cuts[0].coords) == 2
        assert core.buffer(1e-7).covers(cuts[0])
        assert cuts[0].intersection(source.boundary).is_empty
    else:
        assert core.symmetric_difference(wide).area < 1e-6
        assert not cuts
    perimeter, interior, constraints = offset_partitions(source)
    _validate_partition(perimeter + interior, source, convex=True)
    validate_partition_lines(perimeter + interior, constraints)


@pytest.mark.parametrize("core", [
    shapely.union_all([shapely.box(0, 0, 8, 8), shapely.box(8, 3.5, 16, 4.5),
                      shapely.box(16, 0, 24, 8)]),
    shapely.union_all([shapely.box(0, 0, 8, 8), shapely.box(8, 3.5, 20, 4.5)]),
    shapely.union_all([shapely.box(0, 0, 12, 1), shapely.box(11, 0, 12, 12)]),
    shapely.box(0, 0, 20, 20).difference(shapely.box(1, 1, 19, 19)),
])
def test_necks_branches_and_closed_slits(core):
    source = core.buffer(5, join_style="mitre")
    wide, cuts = _collapse_narrow_core(source, core, 2)
    assert cuts
    assert all(core.buffer(1e-7).covers(line) for line in cuts)
    perimeter, interior, cuts = offset_partitions(source)
    _validate_partition(perimeter + interior, source, convex=True)
    perimeter = _merge_tiny_partitions(perimeter, cuts)
    interior = _merge_tiny_partitions(interior, cuts)
    _validate_partition(perimeter + interior, source)
    validate_partition_lines(perimeter + interior, cuts)


def test_empty_core_and_real_courtyard():
    for source in [
        shapely.box(0, 0, 8, 8),
        shapely.box(0, 0, 40, 40).difference(shapely.box(12, 12, 28, 28)),
    ]:
        perimeter, core, cuts = offset_partitions(source)
        _validate_partition(perimeter + core, source, convex=True)
        validate_partition_lines(perimeter + core, cuts)
    perimeter, core, cuts = offset_partitions(shapely.box(0, 0, 8, 8))
    assert len(perimeter) == 1
    assert not core and not cuts


def test_internal_width_configuration_and_disconnected_slits(monkeypatch):
    source = shapely.box(0, 0, 30, 12.5)
    _, core, cuts = offset_partitions(source)
    assert core and not cuts
    monkeypatch.setattr(geom, "CORE_MIN_WIDTH", 3)
    perimeter, core, cuts = offset_partitions(source)
    assert not core and len(cuts) == 1
    validate_partition_lines(perimeter, cuts)
    source = shapely.union_all([source, translate(source, xoff=40)])
    perimeter, core, cuts = offset_partitions(source)
    assert len(cuts) == 2
    assert all(line.length == pytest.approx(20) for line in cuts)
    _validate_partition(perimeter + core, source)
    validate_partition_lines(perimeter + core, cuts)


def test_invalid_footprint_is_reported():
    with pytest.raises(GeometryError, match="valid"):
        offset_partitions(shapely.Polygon([(0, 0), (10, 10), (0, 10), (10, 0)]))


def test_protected_merger_chooses_next_legal_neighbor():
    parts = [shapely.box(0, 0, 4, 2), shapely.box(1, 2, 2, 2.5), shapely.box(2, 2, 4, 4)]
    cuts = [shapely.LineString([(1, 2), (2, 2)])]
    merged = _merge_tiny_partitions(parts, cuts)
    assert len(merged) == 2
    assert any(part.equals(parts[1].union(parts[2])) for part in merged)
    validate_partition_lines(merged, cuts)
    assert len(_merge_tiny_partitions(parts[:2], cuts)) == 2


def test_constraint_validation_rejects_face_crossing_slit():
    with pytest.raises(GeometryError, match="absent"):
        validate_partition_lines(
            [shapely.box(0, 0, 20, 20)], [shapely.LineString([(5, 10), (15, 10)])]
        )


@pytest.mark.parametrize("depth,width", [(0, 2), (-1, 2), (float("nan"), 2), (5, 0)])
def test_invalid_internal_configuration_is_explicit(monkeypatch, depth, width):
    monkeypatch.setattr(geom, "CORE_OFFSET_DEPTH", depth)
    monkeypatch.setattr(geom, "CORE_MIN_WIDTH", width)
    with pytest.raises(ValueError):
        offset_partitions(shapely.box(0, 0, 30, 20))


def test_large_coordinate_translation_preserves_zoning():
    source = shapely.box(0, 0, 30, 11)
    perimeter, core, cuts = offset_partitions(translate(source, xoff=10000, yoff=-25000))
    assert len(cuts) == 1
    assert cuts[0].length == pytest.approx(20)
    _validate_partition(perimeter + core, translate(source, xoff=10000, yoff=-25000))
    validate_partition_lines(perimeter + core, cuts)


@pytest.mark.slow
@pytest.mark.parametrize("divided_zones", [False, True])
def test_insert_core_preserves_virtual_subdivisions_and_space_types(divided_zones):
    case = Path(__file__).parent / "caseFile" / "test2_cortyard.geo"
    model = transform(
        str(case), input_type="geo", stdout=StringIO(),
        options=TransformOptions(insert_core=True, divided_zones=divided_zones),
    )
    assert model.spaceList
    assert any(wall.is_air_boundary for wall in model.wallList)
    assert all(not geometry.faceId.startswith(("core_wall_", "core_face_"))
               for geometry in model.geometryList)
    assert all(space.space_type != "core" for space in model.spaceList)
    for level in sorted({space.level for space in model.spaceList}):
        spaces = [space.force_2d() for space in model.spaceList if space.level == level]
        union = shapely.union_all(spaces)
        assert abs(sum(space.area for space in spaces) - union.area) < 1e-5


def _offset_model(tmp_path, footprint, *, insert_core=True):
    coordinates = np.asarray(footprint.exterior.coords)[:-1]
    rings = [
        ([(x, y, 0) for x, y in coordinates[::-1]], (0, 0, -1)),
        ([(x, y, 3) for x, y in coordinates], (0, 0, 1)),
    ]
    for start, end in zip(coordinates, np.roll(coordinates, -1, axis=0)):
        normal = np.array([end[1] - start[1], start[0] - end[0], 0.0])
        normal /= np.linalg.norm(normal)
        rings.append(([
            (*start, 0), (*end, 0), (*end, 3), (*start, 3),
        ], normal))
    records = []
    for index, (vertices, normal) in enumerate(rings):
        records.extend([f"f,0,{index}", "fn," + ",".join(map(str, normal))])
        records.extend("fv," + ",".join(map(str, point)) for point in vertices)
        records.append(";")
    path = tmp_path / "offset.geo"
    path.write_text("\n".join(records), encoding="utf-8")
    return transform(
        str(path), stdout=StringIO(),
        options=TransformOptions(insert_core=insert_core, divided_zones=True),
    )


@pytest.mark.parametrize("footprint,angle", [
    (shapely.box(0, 0, 30, 20), 0),
    (shapely.box(0, 0, 30, 20), 37),
    (shapely.Polygon([(0, 0), (40, 0), (40, 20), (20, 20), (20, 40), (0, 40)]), 0),
])
def test_actual_core_interfaces_are_opaque_and_other_partitions_remain_air(tmp_path, footprint, angle):
    footprint = rotate(footprint, angle)
    _, core, _ = offset_partitions(footprint)
    expected = shapely.union_all(core).boundary
    model = _offset_model(tmp_path, footprint)
    internal_opaque = [wall for wall in model.wallList if not wall.isOuter and not wall.is_air_boundary]
    actual = shapely.union_all([wall.force_2d() for wall in internal_opaque])
    assert internal_opaque
    assert expected.difference(actual.buffer(0.003)).length < 0.01
    assert actual.difference(expected.buffer(0.003)).length < 0.01
    assert all(len(wall.space) == 2 and not wall.glazingElement for wall in internal_opaque)
    air = shapely.union_all([wall.force_2d() for wall in model.wallList if wall.is_air_boundary])
    assert not air.is_empty
    assert air.intersection(expected).length < 0.01
    if len(core) > 1:
        assert any(shapely.union_all(core).contains(wall.force_2d().interpolate(0.5, normalized=True))
                   for wall in model.wallList if wall.is_air_boundary)
    spaces = [space.force_2d() for space in model.spaceList]
    union = shapely.union_all(spaces)
    assert abs(sum(space.area for space in spaces) - union.area) < 1e-5
    assert union.hausdorff_distance(footprint) < 2 * geom.POINT_PRECISION
    assert abs(union.area - footprint.area) < footprint.length * geom.POINT_PRECISION


@pytest.mark.slow
def test_core_interfaces_export_as_paired_opaque_surfaces(tmp_path):
    model = _offset_model(tmp_path, shapely.box(0, 0, 30, 20))
    opaque = [wall for wall in model.wallList if not wall.isOuter and not wall.is_air_boundary]
    assert opaque
    path = tmp_path / "core.idf"
    exportIDF(model, str(path))
    configure_idd()
    idf = IDF(str(path))
    surfaces = {surface.Name: surface for surface in idf.idfobjects["BUILDINGSURFACE:DETAILED"]}
    ordinary_constructions = {construction.Name for construction in idf.idfobjects["CONSTRUCTION"]}
    for wall in opaque:
        sides = [surfaces[f"{space}-{wall.Uid}"] for space in wall.space]
        for surface, opposite in zip(sides, sides[::-1]):
            assert surface.Construction_Name in ordinary_constructions
            assert surface.Outside_Boundary_Condition == "Surface"
            assert surface.Outside_Boundary_Condition_Object == opposite.Name
            assert surface.Sun_Exposure == "NoSun"
            assert surface.Wind_Exposure == "NoWind"
    assert any(surface.Construction_Name == "Moosas Air Boundary" for surface in surfaces.values())


def test_convex_division_without_core_keeps_internal_air_boundaries(tmp_path):
    footprint = shapely.Polygon([(0, 0), (40, 0), (40, 20), (20, 20), (20, 40), (0, 40)])
    model = _offset_model(tmp_path, footprint, insert_core=False)
    internal = [wall for wall in model.wallList if not wall.isOuter]
    assert internal
    assert all(wall.is_air_boundary for wall in internal)


def test_zero_width_hole_survives_full_space_assembly(tmp_path):
    model = _offset_model(tmp_path, shapely.box(0, 0, 30, 11))
    spaces = [shapely.force_2d(space.force_2d()) for space in model.spaceList]
    assert len(spaces) > 1
    _validate_partition(spaces, shapely.box(0, 0, 30, 11))
    slit = shapely.LineString([(5, 5.5), (25, 5.5)])
    validate_partition_lines(spaces, [slit])
    air_projection = shapely.union_all([
        wall.force_2d() for wall in model.wallList if wall.is_air_boundary
    ])
    assert slit.difference(air_projection.buffer(0.001)).length < 1e-5
