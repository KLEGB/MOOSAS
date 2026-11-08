"""Boundary preparation stage for draft geometry models."""
from __future__ import annotations

from ...model import MoosasModel
from ..geometry.boundary import geometry_arrays, simplify_to_layered_obb
from ..geometry.element import MoosasGeometry
from ..geometry.geos import Vector


def _replace_geometry(model, categories, face_ids, normals, faces, holes):
    geometry_list = []
    for category, face_id, normal, face, face_holes in zip(
        categories, face_ids, normals, faces, holes
    ):
        if isinstance(face_holes, dict):
            face_holes = [face_holes[index] for index in sorted(face_holes)]
        geometry_list.append(
            MoosasGeometry(
                face,
                str(face_id),
                Vector(normal),
                int(category),
                face_holes or [],
                errors="raise",
            )
        )
    model.geometryList = geometry_list
    model.geoId = [geometry.faceId for geometry in geometry_list]
    model.newIndex = len(geometry_list)
    return model


def prepare_boundary_geometry(model: MoosasModel) -> MoosasModel:
    """Simplify a draft model's boundary geometry into layered boxes."""
    categories, face_ids, normals, faces, holes = geometry_arrays(model.geometryList)
    categories, face_ids, normals, faces, holes = simplify_to_layered_obb(
        categories, normals, faces
    )

    return _replace_geometry(model, categories, face_ids, normals, faces, holes)
