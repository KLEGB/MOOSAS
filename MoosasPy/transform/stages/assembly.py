"""Space generation and model assembly stage for transformations."""
from __future__ import annotations

from ...model import MoosasModel
from ...utils import mixItemListToList, np, shapely
from ...utils.constant import geom
from ...utils.tools import searchBy
from ..geometry.cleanse import solveIntersectionHorizontal
from ..geometry.contour import overhead_projection, packing_edges
from ..geometry.element import MoosasEdge, MoosasFace, MoosasFloor, MoosasSkylight, MoosasSpace
from ..geometry.geos import GeometryError, Vector, overlapArea


def assemble_model(
    model: MoosasModel,
    *,
    divided_zones: bool,
    solve_overlap: bool,
    insert_core: bool = False,
) -> MoosasModel:
    """Assemble edges and spaces from a model with generated boundaries."""
    model = packing_edges(model, divided_zones, insert_core=insert_core)
    if solve_overlap:
        model = solveIntersectionHorizontal(model)
    model = pack_model(model, solve_overlap)
    model = pack_attic_spaces(model)
    return identify_basement_spaces(model)


def identify_basement_spaces(model: MoosasModel) -> MoosasModel:
    """Mark spaces whose floor is below the z=0 plane as basements."""
    for space in model.spaceList:
        floor_elevation = space.floor.level + space.floor.offset
        if floor_elevation < -0.5:
            space.space_type = "basement"
    return model


def _horizontal_face_indices(model: MoosasModel, level) -> list[int]:
    """Return only horizontal faces available at a storey level."""
    return [
        index
        for index in searchBy("level", level, model.faceList)
        if abs(float(model.faceList[index].normal[2])) >= 0.99
    ]


def order_connected_walls(walls: list) -> list | None:
    """Order walls into one closed 2D ring without changing their geometry."""
    if len(walls) < 3:
        return None
    remaining = list(walls)
    ordered = [remaining.pop(0)]
    current_end = shapely.get_coordinates(ordered[0].force_2d())[-1]
    tolerance = 2 * geom.POINT_PRECISION
    while remaining:
        for index, candidate in enumerate(remaining):
            coordinates = shapely.get_coordinates(candidate.force_2d())
            if np.linalg.norm(coordinates[0] - current_end) <= tolerance:
                current_end = coordinates[-1]
                ordered.append(remaining.pop(index))
                break
            if np.linalg.norm(coordinates[-1] - current_end) <= tolerance:
                current_end = coordinates[0]
                ordered.append(remaining.pop(index))
                break
        else:
            return None
    first_coordinates = shapely.get_coordinates(ordered[0].force_2d())
    if np.linalg.norm(current_end - first_coordinates[0]) > tolerance and np.linalg.norm(current_end - first_coordinates[-1]) > tolerance:
        return None
    return ordered


def _touches_projection(first, second) -> bool:
    return shapely.distance(first.force_2d(), second.force_2d()) <= geom.POINT_PRECISION


def pack_attic_spaces(model: MoosasModel) -> MoosasModel:
    """Create normal spaces closed by existing inclined roof faces and eave walls."""
    inclined_faces = [
        face for face in model.faceList
        if geom.HORIZONTAL_ANGLE_THRESHOLD <= abs(np.asarray(face.normal, dtype=float)[2]) < 0.99
    ]
    remaining = list(inclined_faces)
    roof_components = []
    while remaining:
        component = [remaining.pop(0)]
        changed = True
        while changed:
            changed = False
            for face in list(remaining):
                if any(_touches_projection(face, component_face) for component_face in component):
                    component.append(face)
                    remaining.remove(face)
                    changed = True
        roof_components.append(component)

    added = []
    used_walls = set()
    for roof_faces in roof_components:
        roof_edges = {edge for face in roof_faces for edge in face.getEdgeStr()}
        eave_walls = [wall for wall in model.wallList if wall not in used_walls and roof_edges.intersection(wall.getEdgeStr())]
        if not eave_walls:
            roof_boundary = shapely.union_all([shapely.boundary(face.force_2d()) for face in roof_faces])
            eave_walls = [
                wall for wall in model.wallList
                if wall not in used_walls
                and shapely.distance(wall.force_2d(), roof_boundary) <= geom.POINT_PRECISION
            ]
        ordered_walls = order_connected_walls(eave_walls)
        if ordered_walls is None:
            continue
        try:
            attic_edge = MoosasEdge(ordered_walls)
        except GeometryError:
            continue

        base_faces = []
        for face in model.faceList:
            if abs(np.asarray(face.normal, dtype=float)[2]) < 0.99 or abs(face.level - attic_edge.level) > geom.LEVEL_MAX_OFFSET:
                continue
            try:
                if overlapArea(face.force_2d(), attic_edge.force_2d()) > geom.AREA_PRECISION:
                    base_faces.append(face)
            except GeometryError:
                continue
        if not base_faces:
            continue
        unique_base_faces = []
        covered = None
        for face in sorted(base_faces, key=lambda item: item.Uid):
            footprint = face.force_2d()
            uncovered = footprint if covered is None else shapely.difference(footprint, covered)
            if shapely.area(uncovered) > geom.AREA_PRECISION:
                unique_base_faces.append(face)
                covered = footprint if covered is None else shapely.union_all([covered, footprint])
        try:
            attic_floor = MoosasFloor(unique_base_faces)
            attic_ceiling = MoosasFloor(roof_faces)
            attic = MoosasSpace(attic_floor, attic_edge, attic_ceiling, space_type="attic")
        except GeometryError:
            continue
        if attic.is_void():
            continue
        model.floorList.append(attic_floor)
        model.ceilingList.append(attic_ceiling)
        model.spaceList.append(attic)
        used_walls.update(ordered_walls)
        added.extend(roof_faces)

    if added:
        added_set = set(added)
        model.face_remain = [face for face in model.face_remain if face not in added_set]
        model.shadingList = np.array([face for face in model.shadingList if face not in added_set])
        print(f"PACKING: Build attic spaces: {len(added_set)} roof faces attached")
    return model


def cap_floor_simple(boundary: shapely.Geometry, level, model: MoosasModel, base_faces: MoosasFloor | None = None) -> MoosasFloor:
    """Fill uncovered floor boundary portions with air-boundary skylight faces."""
    floor_faces: list[MoosasFace] = [] if base_faces is None else base_faces.face
    model.faceList = list(model.faceList)
    model.skylightList = list(model.skylightList)
    covered = shapely.buffer(
        shapely.union_all([face.force_2d() for face in floor_faces]),
        geom.POINT_PRECISION,
    )
    for face in floor_faces:
        face_geometry = shapely.multipolygons(mixItemListToList(face.face))
        boundary = shapely.difference(boundary, face_geometry)

    if not shapely.is_empty(boundary):
        for bound in shapely.get_parts(boundary):
            # Independently snapped partition edges can leave sub-grid coverage seams.
            if shapely.covers(covered, bound):
                continue
            bound = shapely.force_3d(bound, z=level)
            geometry_id = model.includeGeo(bound, Vector([0, 0, 1]).geometry, cat=2)
            model.faceList.append(MoosasFace(model, geometry_id))
            model.skylightList.append(MoosasSkylight(model, geometry_id))
            model.faceList[-1].add_glazing(model.skylightList[-1])
            floor_faces.append(model.faceList[-1])
    return MoosasFloor(floor_faces)


def pack_model(model: MoosasModel, solve_overlap: bool) -> MoosasModel:
    """Match floors and ceilings to edges, then build spaces and voids."""
    # Resolve nested contours before matching or capping physical floors.
    footprints = {edge: edge.force_2d() for edge in model.edgeList}
    parents = {}
    children = {edge: [] for edge in model.edgeList}
    for level in model.levelList:
        edges = [edge for edge in model.edgeList if edge.level == level]
        for child in edges:
            enclosing = [
                parent for parent in edges if parent is not child
                and shapely.contains_properly(footprints[parent], footprints[child])
            ]
            if enclosing:
                parent = min(enclosing, key=lambda edge: footprints[edge].area)
                parents[child] = parent
                children[parent].append(child)
    footprints = {
        edge: shapely.difference(
            footprint, shapely.union_all([footprints[child] for child in children[edge]]),
        )
        for edge, footprint in footprints.items()
    }
    # Snapshot real cover before missing floors are filled with air boundaries.
    overhead = {level: overhead_projection(model, level) for level in model.levelList}
    covered_edges = {
        edge for edge in model.edgeList
        if shapely.area(shapely.intersection(
            footprints[edge], overhead[edge.level], grid_size=geom.POINT_PRECISION
        )) > geom.POINT_PRECISION ** 2
    }
    print("\rPACKING: Match floors", end="")
    remaining_faces = set(model.faceList)
    topology = [{"floor": None, "ceiling": None} for _ in model.edgeList]
    progress = 0
    for level in model.levelList:
        edge_indices = searchBy("level", level, model.edgeList)
        face_indices = _horizontal_face_indices(model, level)
        for edge_index in edge_indices:
            progress += 1
            if model.edgeList[edge_index] not in covered_edges:
                continue
            try:
                shadow_area = 0
                matched_faces = []
                for face_index in face_indices:
                    intersection_area = shapely.area(shapely.intersection(
                        footprints[model.edgeList[edge_index]],
                        model.faceList[face_index].force_2d(),
                        grid_size=geom.POINT_PRECISION,
                    ))
                    if intersection_area > geom.POINT_PRECISION ** 2:
                        shadow_area += intersection_area
                        matched_faces.append(model.faceList[face_index])
                floor = MoosasFloor(matched_faces)
                if solve_overlap and shadow_area < footprints[model.edgeList[edge_index]].area - geom.POINT_PRECISION ** 2:
                    floor = cap_floor_simple(footprints[model.edgeList[edge_index]], level, model, floor)
                topology[edge_index]["floor"] = floor
                model.floorList.append(floor)
                if shadow_area != 0 and floor.area < geom.ROOM_MIN_AREA:
                    print(f"\n******Warning: GeometryError floor faces too small in {floor.Uid}")
                remaining_faces = remaining_faces.difference(set(matched_faces))
                print(f"\rPACKING: Match floors:{progress}/{len(model.edgeList)}\t\t\ttotal floors: {len(model.floorList)}", end="")
            except GeometryError:
                print("\n******Warning: GeometryError: floor faces, pass")
    print()

    print("\rPACKING: Match ceilings", end="")
    progress = 0
    for level, top_level in zip(model.levelList[:-1], model.levelList[1:]):
        edge_indices = searchBy("level", level, model.edgeList)
        face_indices = _horizontal_face_indices(model, top_level)
        for edge_index in edge_indices:
            progress += 1
            if model.edgeList[edge_index] not in covered_edges:
                continue
            try:
                shadow_area = 0
                matched_faces = []
                edge_projection = model.edgeList[edge_index].force_2d(top=True)
                if not shapely.is_valid(edge_projection):
                    edge_projection = footprints[model.edgeList[edge_index]]
                else:
                    edge_projection = shapely.difference(
                        edge_projection,
                        shapely.union_all([
                            child.force_2d(top=True)
                            for child in children[model.edgeList[edge_index]]
                        ]),
                    )
                for face_index in face_indices:
                    intersection_area = shapely.area(shapely.intersection(
                        edge_projection, model.faceList[face_index].force_2d(),
                        grid_size=geom.POINT_PRECISION,
                    ))
                    if intersection_area > geom.POINT_PRECISION ** 2:
                        shadow_area += intersection_area
                        matched_faces.append(model.faceList[face_index])
                if matched_faces:
                    ceiling = MoosasFloor(matched_faces)
                    topology[edge_index]["ceiling"] = ceiling
                    model.ceilingList.append(ceiling)
                    if shadow_area != 0 and ceiling.area < geom.ROOM_MIN_AREA:
                        print(f"******Warning: GeometryError, ceiling faces too small in {ceiling.Uid}")
                remaining_faces = remaining_faces.difference(set(matched_faces))
                print(f"\rPACKING: Match ceilings:{progress}/{len(model.edgeList)}\t\ttotal ceils {len(model.ceilingList)} ", end="")
            except GeometryError:
                print("******Warning: GeometryError, ceiling faces, pass")

    model.face_remain = list(remaining_faces)
    model.shadingList = np.append(model.shadingList, list(remaining_faces))
    spaces = [MoosasSpace(item["floor"], edge, item["ceiling"]) for item, edge in zip(topology, model.edgeList)]
    print()
    spaces_by_edge = {space.edge: space for space in spaces}
    for child, parent in parents.items():
        spaces_by_edge[parent].add_void(MoosasSpace(None, child, None))
    print()
    for space in spaces:
        if shapely.area(shapely.intersection(
            space.force_2d(), overhead[space.level], grid_size=geom.POINT_PRECISION
        )) <= geom.POINT_PRECISION ** 2:
            continue
        if space.is_void():
            model.voidList.append(space)
        else:
            model.spaceList.append(space)
        print(f"\rPACKING: Build a space: {space.id} Bld_level={space.level} Bld_area={space.area:.2f}", end="")
    print()
    return model
