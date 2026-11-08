"""Second-level space and face topology construction."""
from __future__ import annotations

from ...model import MoosasModel
from ...utils import mixItemListToList, np, shapely
from ...utils.constant import geom
from ...utils.tools import searchBy
from ...model.space import MoosasSpace
from ...model.boundary import MoosasEdge, MoosasFloor
from ..geometry.element import MoosasFace, MoosasGlazing, MoosasSkylight, MoosasWall
from ..geometry.geos import GeometryError, Vector, overlapArea
from .glazing import match_face_glazing


def connect_void_spaces(model: MoosasModel) -> MoosasModel:
    """Connect vertically adjacent voids before building space topology."""
    model.faceList = list(model.faceList)
    print("\r2LSB: Checking void connection", end="")
    for index, void in enumerate(model.voidList):
        print(f"\r2LSB: Checking void connection {index}/{len(model.voidList)}", end="")
        if void.floor is None or overlapArea(void.floor.force_2d(), void.edge.force_2d()) <= void.area - geom.AREA_PRECISION:
            continue
        meta_voids = [void]
        while meta_voids[-1] is not None:
            meta_voids.append(find_void_above(meta_voids[-1]))
        meta_voids.pop()
        if not meta_voids or meta_voids[-1].ceiling is None:
            continue
        source_voids = meta_voids
        meta_voids = [MoosasSpace(item.floor, item.edge, item.ceiling) for item in meta_voids]
        for space_bottom, space_top in zip(meta_voids[:-1], meta_voids[1:]):
            space_bottom.ceiling, space_top.floor = find_co_ceiling(space_bottom, space_top)
            model.ceilingList.append(space_bottom.ceiling)
            model.floorList.append(space_top.floor)
        if any(space.is_void() for space in meta_voids):
            continue
        model.spaceList = list(np.append(model.spaceList, meta_voids))
        model.voidList = list(set(model.voidList) - set(source_voids))
    print()
    return model


def build_space_topology(model: MoosasModel) -> MoosasModel:
    """Regenerate space IDs and record space-to-space adjacency."""
    model.faceList = list(model.faceList)
    print("\r2LSB: Recording Boundary topology", end="")
    _normalize_wall_faces(model)
    for space in model.spaceList:
        space.neighbor.clear()
        space.regenerateId()
    _normalize_horizontal_faces(model)

    for index, space in enumerate(model.spaceList):
        print(f"\r2LSB: Recording Boundary topology {index}/{len(model.spaceList)}", end="")
        elements = space.getAllFaces(to_dict=True)
        for element in elements["MoosasWall"]:
            if len(element.space) > 1:
                element.isOuter = False
                neighbor_id = element.space[1] if element.space[0] == space.id else element.space[0]
                space.add_neighbor(neighbor_id, element)
        for element in elements["MoosasFloor"]:
            if len(element.space) > 1:
                element.isOuter = False
                neighbor_id = element.space[1] if element.space[0] == space.id else element.space[0]
                space.add_neighbor(neighbor_id, element)
                model.spaceIdDict[neighbor_id].add_neighbor(space.id, element)
    print()
    return model


def _normalize_wall_faces(model: MoosasModel) -> None:
    """Node active wall projections and share coincident wall segments."""
    spaces = list(model.spaceList) + list(model.voidList)
    spaces += [void for space in model.spaceList for void in space.void]
    edges = list(dict.fromkeys(space.edge for space in spaces))
    walls = list(dict.fromkeys(wall for edge in edges for wall in edge.wall))
    groups = {}
    for wall in walls:
        bounds = (wall.level + wall.offset, wall.toplevel + wall.topoffset)
        groups.setdefault(bounds, []).append(wall)

    replacements = {}
    for group in groups.values():
        projections = [wall.force_2d() for wall in group]
        noded = shapely.union_all(projections, grid_size=geom.POINT_PRECISION)
        coordinates = np.unique(shapely.get_coordinates(noded), axis=0)
        points = shapely.points(coordinates)
        canonical = {}
        # A physical wall must take precedence over a coincident virtual cut.
        for wall in sorted(group, key=lambda item: item.is_air_boundary):
            line = wall.force_2d()
            endpoints = shapely.points(shapely.get_coordinates(line))
            interior = points[
                shapely.dwithin(points, line, geom.POINT_PRECISION / 10)
                & ~shapely.dwithin(points, endpoints[0], geom.POINT_PRECISION)
                & ~shapely.dwithin(points, endpoints[-1], geom.POINT_PRECISION)
            ]
            parts = MoosasWall.break_(wall, list(interior)) if len(interior) else [wall]
            replacements[wall] = []
            for part in parts:
                key = shapely.to_wkb(shapely.normalize(part.force_2d()))
                if key not in canonical:
                    canonical[key] = part
                else:
                    shared = canonical[key]
                    if shared is not part and shared.is_air_boundary == part.is_air_boundary:
                        shared.dissolve(part)
                replacements[wall].append(canonical[key])

    rebuilt_edges = {}
    for edge in edges:
        parts = list(dict.fromkeys(
            part for wall in edge.wall for part in replacements[wall]
        ))
        rebuilt = MoosasEdge(parts, boundary=edge.force_2d())
        rebuilt.Uid = edge.Uid
        rebuilt.internalMass = list(edge.internalMass)
        rebuilt_edges[edge] = rebuilt
    for space in spaces:
        space.edge = rebuilt_edges[space.edge]
    model.edgeList = [rebuilt_edges.get(edge, edge) for edge in model.edgeList]
    active = list(dict.fromkeys(part for parts in replacements.values() for part in parts))
    for wall in active:
        wall.space = []
        wall.isOuter = True
        wall.neighbor.clear()
        for glazing in wall.glazingElement:
            glazing.space = []
    old_walls = set(walls)
    model.wallList = [wall for wall in model.wallList if wall not in old_walls] + active
    old_glazing = {
        glazing for wall in walls if replacements[wall] != [wall]
        for glazing in wall.glazingElement
    }
    active_glazing = {glazing for wall in active for glazing in wall.glazingElement}
    model.glazingList = [
        glazing for glazing in model.glazingList
        if glazing not in old_glazing or glazing in active_glazing
    ]


def _normalize_horizontal_faces(model: MoosasModel) -> None:
    """Merge duplicate two-sided horizontal faces and order their spaces."""
    face_roles = {}
    for space in model.spaceList:
        for role, floor in (("floor", space.floor), ("ceiling", space.ceiling)):
            if floor is None:
                continue
            for face in floor.face:
                if abs(float(face.normal[2])) < 0.99:
                    continue
                face_roles.setdefault(id(face), [face, []])[1].append((space, role))

    horizontal_faces = [value[0] for value in face_roles.values()]
    face_groups = {}
    for face in horizontal_faces:
        level_key = round(
            float(face.level + face.offset) / geom.POINT_PRECISION
        )
        geometry_key = shapely.to_wkb(shapely.normalize(face.force_2d()))
        face_groups.setdefault((level_key, geometry_key), []).append(face)
    consumed = set()
    replacements = {}
    for index, face in enumerate(horizontal_faces):
        if id(face) in consumed:
            continue
        roles = face_roles[id(face)][1]
        level_key = round(
            float(face.level + face.offset) / geom.POINT_PRECISION
        )
        geometry_key = shapely.to_wkb(shapely.normalize(face.force_2d()))
        candidates = [
            other for other in face_groups[(level_key, geometry_key)]
            if id(other) not in consumed
            and float(np.dot(face.normal, other.normal)) < -0.99
        ]
        if not candidates:
            _order_horizontal_face_spaces(face, roles)
            continue
        other = max(candidates, key=lambda item: overlapArea(face.force_2d(), item.force_2d()))
        other_roles = face_roles[id(other)][1]
        face_space_roles = roles + other_roles
        space_ids = {space.id for space, _ in face_space_roles}
        if len(space_ids) != 2:
            _order_horizontal_face_spaces(face, roles)
            continue
        lower = min(
            (space for space, _ in face_space_roles),
            key=lambda space: space.floor.level + space.floor.offset,
        )
        upper = max(
            (space for space, _ in face_space_roles),
            key=lambda space: space.floor.level + space.floor.offset,
        )
        if lower is upper or lower.id == upper.id:
            _order_horizontal_face_spaces(face, roles)
            continue
        if not (
            any(space is lower and role == "ceiling" for space, role in face_space_roles)
            and any(space is upper and role == "floor" for space, role in face_space_roles)
        ):
            _order_horizontal_face_spaces(face, roles)
            continue
        canonical = face if float(face.normal[2]) > 0 else other
        duplicate = other if canonical is face else face
        canonical.space = [lower.id, upper.id]
        canonical.isOuter = False
        replacements[id(duplicate)] = canonical
        consumed.update((id(face), id(other)))
        _order_horizontal_face_spaces(canonical, [(lower, "ceiling"), (upper, "floor")])

    if replacements:
        for space in model.spaceList:
            for floor in (space.floor, space.ceiling):
                if floor is not None:
                    floor.face = [
                        replacements.get(id(face), face)
                        for face in floor.face
                    ]
        model.faceList = [
            replacements.get(id(face), face)
            for face in model.faceList
            if id(face) not in replacements
        ]


def _order_horizontal_face_spaces(face, roles) -> None:
    """Store horizontal face spaces from the lower side to the upper side."""
    spaces = {space.id: space for space, _ in roles}
    if len(spaces) < 2:
        face.space = list(spaces)
        return
    ordered = sorted(
        spaces.values(),
        key=lambda space: space.floor.level + space.floor.offset,
    )
    face.space = [space.id for space in ordered]


def build_face_topology(model: MoosasModel) -> MoosasModel:
    """Record face-to-face adjacency through shared geometric edges."""
    edges = {}
    faces = model.getAllFaces(dumpUseless=True)
    faces = list(faces["MoosasWall"]) + list(faces["MoosasFace"])
    for index, face in enumerate(faces):
        print(f"\r2LSB: Extracting Faces topology {index}/{len(faces)}", end="")
        if not isinstance(face, MoosasGlazing) or isinstance(face, MoosasSkylight):
            for edge in face.getEdgeStr():
                edges.setdefault(edge, []).append(face)

    print()
    for index, edge in enumerate(edges):
        print(f"\r2LSB: Extracting Faces topology {index}/{len(edges)}", end="")
        edge_faces = edges[edge]
        for first_index, first_face in enumerate(edge_faces[:-1]):
            first_face.neighbor[edge] = []
            for second_face in edge_faces[first_index + 1:]:
                first_face.neighbor[edge].append(second_face.Uid)
                second_face.neighbor.setdefault(edge, []).append(first_face.Uid)
    print()
    return model


def cap_floor(boundary: shapely.Geometry, level, model: MoosasModel, base_floor: MoosasFloor | None = None) -> MoosasFloor:
    """Split floor faces by a boundary and create any required aperture faces."""
    floor_faces = []
    remaining_faces = []
    aperture_faces = [] if base_floor is None else base_floor.glazingElement
    base_faces = [] if base_floor is None else base_floor.face
    for face in base_faces:
        geometry = shapely.multipolygons(mixItemListToList(face.face))
        useful = shapely.intersection(boundary, geometry, grid_size=geom.POINT_PRECISION)
        if shapely.is_empty(useful):
            continue
        for item in shapely.get_parts(useful):
            item = shapely.force_3d(item, z=level)
            if shapely.get_dimensions(item) != 2:
                continue
            geometry_id = model.includeGeo(item, Vector([0, 0, 1]).geometry, cat=0)
            model.faceList.append(MoosasFace(model=model, faceId=geometry_id))
            floor_faces.append(model.faceList[-1])
        remaining = shapely.difference(geometry, boundary, grid_size=geom.POINT_PRECISION)
        for item in shapely.get_parts(remaining):
            item = shapely.force_3d(item, z=level)
            if shapely.get_dimensions(item) != 2:
                continue
            geometry_id = model.includeGeo(item, Vector([0, 0, 1]).geometry, cat=0)
            model.faceList.append(MoosasFace(model, geometry_id))
            remaining_faces.append(model.faceList[-1])
    for face in floor_faces:
        boundary = shapely.difference(boundary, shapely.multipolygons(mixItemListToList(face.face)))
    for item in shapely.get_parts(boundary):
        try:
            item = shapely.force_3d(item, z=level)
            geometry_id = model.includeGeo(item, Vector([0, 0, 1]).geometry, cat=2)
            model.faceList.append(MoosasFace(model, geometry_id))
            model.skylightList.append(MoosasSkylight(model, geometry_id))
            model.faceList[-1].add_glazing(model.skylightList[-1])
            floor_faces.append(model.faceList[-1])
        except GeometryError:
            continue
    for glazing in aperture_faces:
        for face in floor_faces:
            if match_face_glazing(face, glazing):
                break
        else:
            for face in remaining_faces:
                if match_face_glazing(face, glazing):
                    break
    return MoosasFloor(floor_faces)


def find_void_above(void_with_floor: MoosasSpace) -> MoosasSpace | None:
    """Find the unbounded void directly above a bounded void."""
    model = void_with_floor.parent
    level_index = model.levelList.index(void_with_floor.level)
    if level_index == len(model.levelList) - 1:
        return None
    top_level = model.levelList[level_index + 1]
    top_voids = np.array(model.voidList)[searchBy("level", top_level, model.voidList)]
    for top_void in top_voids:
        if top_void.floor is None and (
            shapely.contains(void_with_floor.force_2d(top=True), top_void.force_2d())
            or shapely.contains(top_void.force_2d(), void_with_floor.force_2d(top=True))
        ):
            return top_void
    return None


def find_co_ceiling(space_bottom: MoosasSpace, space_top: MoosasSpace) -> tuple[MoosasFloor, MoosasFloor]:
    """Rebuild floor and ceiling portions around the overlap of stacked void spaces."""
    model = space_bottom.parent
    elevation = space_top.edge.elevation
    shared_faces = list(set(space_bottom.ceiling.face if space_bottom.ceiling else []) | set(space_top.floor.face if space_top.floor else []))
    intersection = shapely.intersection(space_bottom.force_2d(True), space_top.force_2d(), grid_size=geom.POINT_PRECISION)
    print(space_bottom.force_2d(True), space_top.force_2d(), intersection)
    ceiling_geometry = shapely.force_3d(shapely.difference(space_bottom.force_2d(True), intersection, grid_size=geom.POINT_PRECISION), z=elevation)
    floor_geometry = shapely.force_3d(shapely.difference(space_top.force_2d(), intersection, grid_size=geom.POINT_PRECISION), z=elevation)
    if shapely.is_empty(intersection):
        raise Exception("space disjoint")
    intersection_floor = cap_floor(space_top.force_2d(), space_top.level, model, MoosasFloor(shared_faces))
    floor_faces = _cap_remaining(floor_geometry, space_top, model, intersection_floor)
    ceiling_faces = _cap_remaining(ceiling_geometry, space_top, model, intersection_floor)
    return MoosasFloor(ceiling_faces), MoosasFloor(floor_faces)


def _cap_remaining(geometry, space_top: MoosasSpace, model: MoosasModel, intersection_floor: MoosasFloor):
    if shapely.is_empty(geometry):
        return intersection_floor.face
    included = []
    for face in mixItemListToList(shapely.get_parts(geometry)):
        try:
            geometry_id = model.includeGeo(face, Vector([0, 0, 1]).geometry)
            included.append(MoosasFace(model, geometry_id))
        except GeometryError:
            continue
    capped = cap_floor(space_top.force_2d(), space_top.level, model, MoosasFloor(included))
    return capped.face + intersection_floor.face
