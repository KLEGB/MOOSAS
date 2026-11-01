"""Geometry operations applied to :class:`MoosasModel` instances."""
from __future__ import annotations

from ..transform.geometry.element import (
    MoosasFace,
    MoosasGlazing,
    MoosasSkylight,
    MoosasWall,
    glazing_matches_parent,
)
from ..transform.geometry.geos import Vector
from ..utils import mixItemListToList, np, shapely


def move_opaque(model, target, distance: float):
    """Move one opaque element along its face normal."""
    if not isinstance(distance, (int, float)) or not np.isfinite(distance):
        raise ValueError("distance must be a finite number")

    if isinstance(target, str):
        matches = [element for element in model.getAllFaces() if element.Uid == target]
        if not matches:
            matches = [
                element for element in model.getAllFaces()
                if target in mixItemListToList(element.faceId)
            ]
        if len(matches) != 1:
            raise ValueError(f"opaque target {target!r} was not found uniquely")
        target = matches[0]

    if not isinstance(target, (MoosasWall, MoosasFace)):
        raise TypeError("target must be a MoosasWall, MoosasFace, Uid, or faceId")
    if target.category not in (0, 3, 4):
        raise ValueError("target must be opaque")

    displacement = np.asarray(target.normal, dtype=float) * float(distance)
    target_points = shapely.get_coordinates(target.face, include_z=True)
    linked_geometries = set(target.geometry)
    for glazing in target.glazingElement:
        linked_geometries.update(glazing.geometry)

    changed_geometries = set()
    for geometry in model.geometryList:
        if geometry in linked_geometries:
            geometry.move_points(
                shapely.get_coordinates(geometry.face, include_z=True),
                displacement,
            )
            changed_geometries.add(geometry)
        elif geometry.move_points(target_points, displacement):
            changed_geometries.add(geometry)

    affected_elements = [
        element for element in model.getAllFaces()
        if any(geometry in changed_geometries for geometry in element.geometry)
    ]
    for element in affected_elements:
        if isinstance(element, MoosasWall):
            element.prepareProjection()
            element.orientation = Vector(element.normal)

    for space in model.spaceList + model.voidList:
        space.clear_neighbors()
        if space.edge is not None:
            space.edge.prepareBoundary()

    for space in model.spaceList:
        elements = space.getAllFaces(to_dict=True)
        for element in elements["MoosasWall"] + elements["MoosasFloor"]:
            if len(element.space) > 1:
                element.isOuter = False
                neighbor_id = (
                    element.space[1]
                    if element.space[0] == space.id
                    else element.space[0]
                )
                space.add_neighbor(neighbor_id, element)
                if neighbor_id in model.spaceIdDict:
                    model.spaceIdDict[neighbor_id].add_neighbor(space.id, element)

    from ..transform.stages.topology import build_face_topology

    build_face_topology(model)
    return model


def move_glazing(model, target, displacement):
    """Move a wall window or roof skylight within its parent face."""
    displacement = np.asarray(displacement, dtype=float)
    if displacement.shape != (3,) or not np.isfinite(displacement).all():
        raise ValueError("displacement must be a finite three-dimensional vector")

    if isinstance(target, str):
        matches = [
            element for element in model.glazingList + model.skylightList
            if element.Uid == target or target in mixItemListToList(element.faceId)
        ]
        if len(matches) != 1:
            raise ValueError(f"glazing target {target!r} was not found uniquely")
        target = matches[0]

    if not isinstance(target, (MoosasGlazing, MoosasSkylight)):
        raise TypeError("target must be a MoosasGlazing, MoosasSkylight, Uid, or faceId")

    parent = getattr(target, "parentFace", None)
    if parent is None:
        parent_candidates = [
            element for element in model.getAllFaces()
            if target in element.glazingElement
        ]
        if len(parent_candidates) != 1:
            raise ValueError("glazing target must have one parent wall or roof face")
        parent = parent_candidates[0]

    parent_normal = np.asarray(parent.normal, dtype=float)
    normal_length = np.linalg.norm(parent_normal)
    if normal_length <= 0:
        raise ValueError("parent face has an invalid normal")
    if abs(float(np.dot(displacement, parent_normal / normal_length))) > 1e-8:
        raise ValueError("glazing displacement must be tangent to its parent face")

    geometries = list(target.geometry)
    for geometry in geometries:
        geometry.move_points(
            shapely.get_coordinates(geometry.face, include_z=True),
            displacement,
        )

    if not glazing_matches_parent(parent, target):
        for geometry in geometries:
            geometry.move_points(
                shapely.get_coordinates(geometry.face, include_z=True),
                -displacement,
            )
        raise ValueError("glazing displacement would move the glazing outside its parent face")

    if isinstance(target, MoosasGlazing):
        target.prepareProjection()
        target.orientation = Vector(target.normal)
    return model
