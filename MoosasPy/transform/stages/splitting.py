"""Geometry splitting operations used by the transformation pipeline."""
from __future__ import annotations

import shapely

from ...model import MoosasModel
from ...utils import np
from ...utils.constant import geom
from ..geometry.cleanse import cleanseInvalidWall, solveIntersectionVertical
from ..geometry.element import MoosasGeometry
from ..geometry.geos import GeometryError, Vector, splitOnZ


def split_vertical_walls(model: MoosasModel, excluded_face_ids=()) -> MoosasModel:
    """Split source vertical geometry at intervening building levels."""
    excluded_ids = set(excluded_face_ids)
    wall_ids = [
        geometry.faceId
        for geometry in model.geometryList
        if np.abs(Vector.dot(geometry.normal, shapely.points([0, 0, 1]))) < geom.HORIZONTAL_ANGLE_THRESHOLD
    ]
    for index, face_id in enumerate(wall_ids):
        if face_id in excluded_ids:
            continue
        model = split_vertical_face(model, face_id)
        print(f"\rLOADING: Break walls {index + 1}/{len(wall_ids)}", end="")
    updated_wall_ids = [
        geometry.faceId
        for geometry in model.geometryList
        if np.abs(Vector.dot(geometry.normal, shapely.points([0, 0, 1]))) < geom.HORIZONTAL_ANGLE_THRESHOLD
    ]
    print(f"\t\t\tadd walls:{len(updated_wall_ids) - len(wall_ids)}")
    return model


def split_vertical_face(model: MoosasModel, face_id) -> MoosasModel:
    """Split one vertical face at each intervening building level."""
    geometry: MoosasGeometry = model.geometryList[model.geoId.index(face_id)]
    category = geometry.category
    face = geometry.face
    elevations = [coordinate[2] for coordinate in shapely.get_coordinates(face, include_z=True)]

    top = np.max(elevations)
    bottom = np.min(elevations)
    bottom_level, top_level = -1, len(model.levelList) - 1
    for level_index, level in enumerate(model.levelList):
        if bottom >= level:
            bottom_level = level_index
        if top >= level:
            top_level = level_index

    if bottom_level == len(model.levelList) - 1 or top <= model.levelList[bottom_level + 1]:
        return model

    finished_faces = []
    working_faces = [face]
    while working_faces:
        current_face = working_faces.pop()
        is_smallest_face = True
        elevations = [coordinate[2] for coordinate in shapely.get_coordinates(current_face, include_z=True)]
        for level in model.levelList[bottom_level + 1:top_level + 1]:
            if np.min(elevations) + geom.POINT_PRECISION < level < np.max(elevations) - geom.POINT_PRECISION:
                split_faces = splitOnZ(current_face, level)
                if split_faces is not None and len(split_faces[0]) * len(split_faces[1]) > 0:
                    working_faces = list(np.append(working_faces, split_faces[0]))
                    working_faces = list(np.append(working_faces, split_faces[1]))
                    is_smallest_face = False
                    break
        if is_smallest_face:
            finished_faces.append(current_face)

    if len(finished_faces) > 1:
        model.removeGeo(face_id)
        for split_face in finished_faces:
            try:
                model.includeGeo(split_face, cat=category)
            except GeometryError:
                pass
    return model


def split_wall_intersections(model: MoosasModel) -> MoosasModel:
    """Split intersecting wall projections and remove invalid walls."""
    model = solveIntersectionVertical(model)
    return cleanseInvalidWall(model)
