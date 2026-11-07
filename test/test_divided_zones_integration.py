from functools import lru_cache
from io import StringIO
import math
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import numpy as np
import pytest

from MoosasPy.simulation.energy.runner import EnergyRunner
from MoosasPy.simulation.weather import Location
from MoosasPy.simulation.weather.epw import read_weather_csv
from MoosasPy.transform import TransformOptions, transform
from MoosasPy.model.space import MoosasSpace
from MoosasPy.utils import shapely
from MoosasPy.utils.constant import geom


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CASE_DIRECTORY = PROJECT_ROOT / "test" / "caseFile"
BEIJING_WEATHER = PROJECT_ROOT / "MoosasPy" / "db" / "weather" / "545110.csv"


@lru_cache(maxsize=None)
def _transform_divided_case(case_name):
    return transform(
        str(CASE_DIRECTORY / case_name),
        input_type="geo",
        stdout=StringIO(),
        options=TransformOptions(divided_zones=True),
    )


def test_attic_is_preserved_without_divided_zones():
    model = transform(
        str(CASE_DIRECTORY / "test90_restockwithattic.geo"),
        input_type="geo",
        stdout=StringIO(),
        options=TransformOptions(divided_zones=False),
    )

    assert sum(space.space_type == "attic" for space in model.spaceList) == 1


def test_duplicate_walls_are_removed_before_shading_attachment():
    model = transform(
        str(CASE_DIRECTORY / "test90_restockwithattic.geo"),
        input_type="geo",
        stdout=StringIO(),
        options=TransformOptions(divided_zones=False),
    )

    for first_index, first_wall in enumerate(model.wallList):
        for second_wall in model.wallList[first_index + 1:]:
            assert not (
                first_wall.level == second_wall.level
                and first_wall.toplevel == second_wall.toplevel
                and first_wall.is_air_boundary == second_wall.is_air_boundary
                and shapely.equals(first_wall.force_2d(), second_wall.force_2d())
            )


def test_inclined_roof_faces_are_not_room_ceilings():
    model = transform(
        str(CASE_DIRECTORY / "test90_restockwithattic.geo"),
        input_type="geo",
        stdout=StringIO(),
        options=TransformOptions(divided_zones=False),
    )

    room_spaces = [
        space
        for space in model.spaceList
        if space.space_type == "room" and space.level == 0
    ]
    roof_ids = {"f_37", "f_38", "f_39", "f_40"}

    assert room_spaces
    assert all(
        roof_ids.isdisjoint({str(face.faceId) for face in space.ceiling.face})
        for space in room_spaces
    )

    attic = next(space for space in model.spaceList if space.space_type == "attic")
    assert roof_ids <= {str(face.faceId) for face in attic.ceiling.face}


def test_roof_classification_accepts_a_30_degree_slope():
    target_roof_normal_z = math.cos(math.radians(30.21))

    assert target_roof_normal_z >= geom.HORIZONTAL_ANGLE_THRESHOLD


def test_attic_is_not_rejected_by_standard_room_height():
    space = object.__new__(MoosasSpace)
    space.floor = SimpleNamespace(level=2.43, offset=0.0)
    space.ceiling = SimpleNamespace(level=3.2076, offset=0.0)
    space.space_type = "room"

    assert space.is_void()

    space.space_type = "attic"

    assert not space.is_void()


def test_divided_zone_space_center_uses_boundary_centroid():
    model = transform(
        str(CASE_DIRECTORY / "test90_restockwithattic.geo"),
        input_type="geo",
        stdout=StringIO(),
        options=TransformOptions(divided_zones=True),
    )

    attic = next(space for space in model.spaceList if space.space_type == "attic")
    centroid = shapely.centroid(attic.force_2d())
    center = attic.getWeightCenter()

    assert center[:2] == pytest.approx((centroid.x, centroid.y))
    assert center[2] == pytest.approx(attic.floor.level + attic.height / 2.0)


@pytest.mark.parametrize(
    "case_name",
    (
        "test0_6spacesIntersection.geo",
        pytest.param("test2_cortyard.geo", marks=pytest.mark.slow),
        "test4_skylight.geo",
    ),
)
def test_divided_zone_cases_build_simulation_ready_topology(case_name):
    model = _transform_divided_case(case_name)

    space_ids = [str(space.id) for space in model.spaceList]
    assert space_ids
    assert len(space_ids) == len(set(space_ids))
    assert all(space.area > 0 for space in model.spaceList)
    assert all(
        len({str(space_id) for space_id in wall.space}) == 2
        for wall in model.wallList
        if wall.is_air_boundary
    )


def test_courtyard_case_generates_two_sided_air_boundaries():
    model = _transform_divided_case("test2_cortyard.geo")
    air_walls = [wall for wall in model.wallList if wall.is_air_boundary]

    assert len(model.spaceList) == 53
    assert len(air_walls) == 18
    assert all(len({str(space_id) for space_id in wall.space}) == 2 for wall in air_walls)


@pytest.mark.parametrize("divided", [False, True])
def test_narrow_physical_protrusion_is_not_lost_after_partitioning(tmp_path, divided):
    footprint = [
        (0, 0), (10, 0), (10, 10), (6, 10),
        (6, 11), (5.5, 11), (5.5, 10), (0, 10),
    ]
    faces = []

    def add_face(name, points, normal):
        faces.append("\n".join(
            [f"f,0,{name}", "fn," + ",".join(map(str, normal))]
            + ["fv," + ",".join(map(str, point)) for point in points]
            + [";"]
        ))

    for level in (0, 3):
        add_face(f"slab_{level}", [(x, y, level) for x, y in footprint], (0, 0, 1))
    for index, (start, end) in enumerate(zip(footprint, footprint[1:] + footprint[:1])):
        dx, dy = end[0] - start[0], end[1] - start[1]
        add_face(f"wall_{index}", [(*start, 0), (*end, 0), (*end, 3), (*start, 3)],
                 (dy, -dx, 0))
    source = tmp_path / "narrow_protrusion.geo"
    source.write_text("\n".join(faces), encoding="utf-8")
    model = transform(
        str(source), stdout=StringIO(),
        options=TransformOptions(divided_zones=divided),
    )

    assert sum(space.area for space in model.spaceList) == pytest.approx(100.5)
    assert not model.voidList
    assert all(not space.is_open() for space in model.spaceList)
    assert all(space.floor.area >= space.area - geom.POINT_PRECISION ** 2
               and space.ceiling.area >= space.area - geom.POINT_PRECISION ** 2
               for space in model.spaceList)
    if divided:
        assert all(space.area >= geom.ROOM_MIN_AREA for space in model.spaceList)


@pytest.mark.parametrize("ground_hole", [False, True])
@pytest.mark.parametrize("divided", [False, True])
@pytest.mark.parametrize("roof_category", [None, 0, 1, -1, 2])
def test_courtyard_requires_a_physical_overhead_cover(tmp_path, divided, roof_category, ground_hole):
    outer = [(0, 0), (20, 0), (20, 20), (0, 20)]
    inner = [(5, 5), (15, 5), (15, 10), (10, 10), (10, 15), (5, 15)]
    courtyard = shapely.Polygon(inner)
    faces = []

    def add_face(points, normal, name, category=0, hole=()):
        lines = [f"f,{category},{name}", "fn," + ",".join(map(str, normal))]
        lines.extend("fv," + ",".join(map(str, point)) for point in points)
        lines.extend("fh,0," + ",".join(map(str, point)) for point in hole)
        faces.append("\n".join(lines + [";"]))

    for z in (0, 3, 6):
        add_face(
            [(x, y, z) for x, y in outer], (0, 0, 1), f"slab_{z}",
            hole=[(x, y, z) for x, y in inner] if z or ground_hole else (),
        )
    if roof_category is not None:
        add_face([(x, y, 6) for x, y in inner], (0, 0, 1), "cover", roof_category)
    for ring_index, ring in enumerate((outer, inner)):
        for index, (start, end) in enumerate(zip(ring, ring[1:] + ring[:1])):
            dx, dy = end[0] - start[0], end[1] - start[1]
            add_face(
                [(*start, 0), (*end, 0), (*end, 6), (*start, 6)],
                (dy, -dx, 0), f"wall_{ring_index}_{index}",
            )
    source = tmp_path / "courtyard.geo"
    source.write_text("\n".join(faces), encoding="utf-8")

    model = transform(
        str(source), stdout=StringIO(),
        options=TransformOptions(divided_zones=divided, attach_shading=True),
    )
    covered = roof_category in (0, 1)
    courtyard_spaces = [
        space for space in model.spaceList
        if shapely.area(shapely.intersection(space.force_2d(), courtyard)) > geom.AREA_PRECISION
    ]

    assert bool(courtyard_spaces) == covered
    assert all(
        shapely.area(shapely.intersection(void.force_2d(), courtyard)) <= geom.AREA_PRECISION
        for void in model.voidList
    )
    assert sum(space.area for space in model.spaceList) == pytest.approx(
        800.0 if covered else 650.0
    )
    if not covered:
        assert all(not space.is_open() for space in model.spaceList)
        assert not model.skylightList
        for wall in model.wallList:
            assert shapely.length(
                shapely.intersection(wall.force_2d(), courtyard)
                .difference(shapely.boundary(courtyard))
            ) <= geom.POINT_PRECISION
    if divided:
        assert all(
            len(set(wall.space)) == 2 for wall in model.wallList if wall.is_air_boundary
        )


def test_divided_zones_preserve_explicit_shading_geometry():
    source = (CASE_DIRECTORY / "test0_6spacesIntersection.geo").read_text(encoding="utf-8")
    shading = """\
f,-1,preserved_shading
fn,0.447,0.000,-0.894
fv,0.000,-2.000,20.000
fv,4.000,-2.000,22.000
fv,4.000,-4.000,22.000
fv,0.000,-4.000,20.000
fh,0,1.000,-2.500,20.500
fh,0,3.000,-2.500,21.500
fh,0,3.000,-3.500,21.500
fh,0,1.000,-3.500,20.500
;
"""
    with TemporaryDirectory() as directory:
        source_path = Path(directory) / "divided-with-shading.geo"
        source_path.write_text(f"{source.rstrip()}\n{shading}", encoding="utf-8")
        model = transform(
            str(source_path),
            input_type="geo",
            stdout=StringIO(),
            options=TransformOptions(divided_zones=True, attach_shading=True),
        )

    preserved = next(item for item in model.shadingList if item.firstFaceId == "preserved_shading")
    geometry = preserved.geometry[0]
    rings = shapely.get_rings(geometry.face)
    outer = shapely.get_coordinates(rings[0], include_z=True)[:-1]
    hole = shapely.get_coordinates(rings[1], include_z=True)[:-1]

    assert geometry.category == -1
    assert np.ptp(outer[:, 2]) == pytest.approx(2.0)
    assert np.ptp(hole[:, 2]) == pytest.approx(1.0)


@pytest.mark.skipif(
    not BEIJING_WEATHER.is_file(),
    reason="requires Beijing 545110 weather data",
)
@pytest.mark.slow
def test_divided_zone_model_runs_energy_simulation():
    model = _transform_divided_case("test0_6spacesIntersection.geo")

    result = EnergyRunner(
        model=model,
        template_id="CN/GB55015-2021/3A/Residential",
        weather=read_weather_csv(
            BEIJING_WEATHER,
            Location("545110", "BEIJING/PEKING", "-", 39.93, 116.28, 55.0, 100962.17),
        ),
    ).run()

    assert len(result.commands) == 1
    assert result.commands[0].returncode == 0
    assert len(result.data["spaces"]) == len(model.spaceList)
    assert len(result.data["months"]) == 12
    totals = {key: float(result.data["total"][key]) for key in ("cooling", "heating", "lighting", "equipment", "total")}
    assert all(math.isfinite(value) and value >= 0 for value in totals.values())
    assert totals["cooling"] == pytest.approx(14.36, abs=0.01)
    assert totals["heating"] == pytest.approx(78.24, abs=0.01)
    assert totals["lighting"] == pytest.approx(7.30, abs=0.01)
    assert totals["equipment"] == pytest.approx(19.42, abs=0.01)
    assert totals["total"] == pytest.approx(sum(totals[key] for key in ("cooling", "heating", "lighting", "equipment")), abs=0.01)
