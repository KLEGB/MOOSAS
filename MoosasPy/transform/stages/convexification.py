"""Model convexification stage for divided-zone transformations."""
from __future__ import annotations

from ...model import MoosasModel
from ...utils import mixItemListToList, shapely
from ..geometry.contour import _merge_tiny_partitions
from ..geometry.convexify import GeometryConvexifier
from ..geometry.element import MoosasGeometry
from ..geometry.geos import Projection, Vector


def convexify_model(model: MoosasModel) -> MoosasModel:
    """Build a convexified geometry-only model from active model elements."""
    geometry_by_id = {geometry.faceId: geometry for geometry in model.geometryList}
    shading_geometry = [geometry for geometry in model.geometryList if geometry.category == -1]
    source_geometry = []
    for element in model.getAllFaces():
        for face_id in mixItemListToList(element.faceId):
            geometry = geometry_by_id.get(face_id)
            if geometry is not None and geometry.category != -1 and geometry not in source_geometry:
                source_geometry.append(geometry)

    horizontal_ids = {
        face_id for face in model.faceList for face_id in mixItemListToList(face.faceId)
    }
    categories = [
        4 if geometry.category == 0 and geometry.faceId in horizontal_ids else geometry.category
        for geometry in source_geometry
    ]
    face_ids = [geometry.faceId for geometry in source_geometry]
    normals = [Vector(geometry.normal).array for geometry in source_geometry]
    faces = [
        shapely.get_coordinates(shapely.get_rings(geometry.face)[0], include_z=True)[:-1]
        for geometry in source_geometry
    ]
    holes = [
        [shapely.get_coordinates(ring, include_z=True)[:-1] for ring in shapely.get_rings(geometry.face)[1:]]
        for geometry in source_geometry
    ]
    convex_categories, convex_ids, convex_normals, convex_faces, _ = GeometryConvexifier.convexify_faces(
        categories,
        face_ids,
        normals,
        faces,
        holes,
    )

    # Re-absorb small or slender pieces of each split horizontal face, while
    # its fragments still share one source's properties.
    groups = {}
    for index, (category, convex_id) in enumerate(zip(convex_categories, convex_ids)):
        if category == 4 and convex_id.startswith("#"):
            groups.setdefault(convex_id[1:convex_id.rindex("_")], []).append(index)
    merged = {}
    for source_id, indices in groups.items():
        projection = Projection.fromPolygon(geometry_by_id[source_id].face)
        parts = _merge_tiny_partitions([
            shapely.force_2d(projection.toUV(shapely.polygons(convex_faces[index]))) for index in indices
        ])
        merged[indices[0]] = [
            (4, source_id if len(parts) == 1 else f"#{source_id}_{number}", convex_normals[indices[0]],
             shapely.get_coordinates(projection.toWorld(part).exterior, include_z=True)[:-1])
            for number, part in enumerate(parts)
        ]
        merged.update({index: [] for index in indices[1:]})
    convex_items = [
        item
        for index, item in enumerate(zip(convex_categories, convex_ids, convex_normals, convex_faces))
        for item in merged.get(index, [item])
    ]

    convex_model = MoosasModel()
    convex_model.buildingTemplate = model.buildingTemplate
    convex_model.schedule = model.schedule
    convex_model.geometryList = [
        MoosasGeometry(
            shapely.polygons(face),
            str(face_id),
            Vector(face_normal),
            int(category),
            [],
            errors="raise",
        )
        for category, face_id, face_normal, face in convex_items
    ] + shading_geometry
    convex_model.geoId = [geometry.faceId for geometry in convex_model.geometryList]
    convex_model.newIndex = model.newIndex
    return convex_model
