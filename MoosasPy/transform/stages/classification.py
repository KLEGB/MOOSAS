"""Geometry classification stage for the transformation pipeline."""
from __future__ import annotations

import shapely

from ...model import MoosasModel
from ...utils import np
from ...utils.constant import geom
from ..geometry.convexify import convexify_shape
from ..geometry.cleanse import cleanseDuplicatedLevel
from ..geometry.element import MoosasElement, MoosasFace, MoosasGlazing, MoosasSkylight, MoosasWall
from ..geometry.geos import GeometryError, Projection, Vector
from .splitting import split_vertical_walls


def classify_model(
    model: MoosasModel,
    triangulate_faces: bool = True,
    break_wall_vertical: bool = True,
    attach_shading: bool = False,
) -> MoosasModel | None:
    """Classify source geometry into building elements and establish levels."""
    model._levels_frozen = False
    classified_ids = []
    predefined_faces = {}
    predefined_walls = []
    for geometry in model.geometryList:
        if geometry.category in (-2, -1):
            classified_ids.append(geometry.faceId)
        elif geometry.category in (4, 6):
            predefined_faces[geometry.faceId] = geometry.category
        elif geometry.category in (3, 5):
            predefined_walls.append((geometry, geometry.category))
            classified_ids.append(geometry.faceId)

    print()
    if triangulate_faces:
        deleted_indices = []
        for index, geometry in enumerate(model.geometryList):
            if geometry.faceId in classified_ids or geometry.faceId in predefined_faces:
                continue
            print(f"\rLOADING: triangulate horizontal faces {index + 1}/{len(model.geoId)}", end="")
            if np.abs(Vector.dot(geometry.normal, shapely.points([0, 0, 1]))) >= geom.HORIZONTAL_ANGLE_THRESHOLD:
                if len(geometry.holes) > 0:
                    projection = Projection.fromPolygon(geometry.face)
                    face_projection = projection.toUV(geometry.face)
                    hole_projections = [projection.toUV(hole) for hole in geometry.holes]
                    split_projections, _ = convexify_shape(face_projection, hole_projections)
                    for split_projection in split_projections:
                        try:
                            model.includeGeo(projection.toWorld(split_projection), cat=geometry.category)
                        except GeometryError as error:
                            print(f"******Warning: {error}")
                    deleted_indices.append(index)

        model.geoId = list(np.delete(model.geoId, deleted_indices))
        model.geometryList = list(np.delete(model.geometryList, deleted_indices))
        print(f"\t\tprocessing faces: {len(deleted_indices)}")

    def load_surface(geometry):
        predefined = predefined_faces.get(geometry.faceId)
        skylight = predefined == 6 if predefined is not None else geometry.category not in (0, 2)
        if predefined is not None:
            geometry.setCategory()
        face = MoosasSkylight(model, geometry) if skylight else MoosasFace(model, geometry)
        (model.skylightList if skylight else model.faceList).append(face)
        classified_ids.append(geometry.faceId)

    # Even explicitly tagged roofs/floors must wait for horizontal references.
    for geometry in model.geometryList:
        if geometry.faceId not in classified_ids and abs(Vector(geometry.normal).array[2]) >= 0.99:
            load_surface(geometry)
    if not model.levelList:
        print("******Warning: no horizontal storey reference found")
        return None
    model = cleanseDuplicatedLevel(model)
    model._levels_frozen = True

    for geometry in model.geometryList:
        if geometry.faceId in classified_ids:
            continue
        nz = abs(Vector(geometry.normal).array[2])
        if geom.HORIZONTAL_ANGLE_THRESHOLD <= nz < 0.99 or geometry.faceId in predefined_faces:
            try:
                load_surface(geometry)
            except GeometryError as error:
                # An unmatched slope stays geometry/shading, never a cutting level.
                classified_ids.append(geometry.faceId)
                print(f"******Warning: {geometry.faceId}: {error}")
                if attach_shading:
                    z = float(np.min(shapely.get_coordinates(geometry.face, include_z=True)[:, 2]))
                    nearest = min(model.levelList, key=lambda level: (abs(level - z), level))
                    model.shadingList.append(MoosasElement(model, geometry, level=nearest, offset=z-nearest))

    for geometry, category in predefined_walls:
        geometry.setCategory()
        (model.wallList if category == 3 else model.glazingList).append(
            MoosasWall(model, geometry) if category == 3 else MoosasGlazing(model, geometry)
        )
    print(f"\t\ttotal horizontal faces: {len(model.faceList)} skylights: {len(model.skylightList)}")

    if break_wall_vertical:
        model = split_vertical_walls(model, classified_ids)

    for index, geometry in enumerate(model.geometryList):
        if geometry.faceId in classified_ids:
            continue
        print(f"\rLOADING: Filtering vertical faces {index + 1}/{len(model.geoId)}", end="")
        if np.abs(Vector.dot(geometry.normal, shapely.points([0, 0, 1]))) < geom.HORIZONTAL_ANGLE_THRESHOLD:
            if geometry.category in (0, 2):
                model.wallList.append(MoosasWall(model, geometry))
            else:
                model.glazingList.append(MoosasGlazing(model, geometry))

    print(f"\t\ttotal vertical faces: {len(model.wallList)} glazings: {len(model.glazingList)}")
    return model
