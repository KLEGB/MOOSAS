"""Convex face decomposition helpers shared by geometry consumers."""

from .convexify import GeometryConvexifier
from .geos import Projection, simplify
from ...utils import np, shapely


def triangulate2dFace(boundary: shapely.Geometry, holes: np.ndarray[shapely.Geometry] = None):
    """Decompose a 2D face, opening optional holes with double cuts before splitting."""
    # MoosasGeometry.face already contains its holes. Flattening every ring
    # into one coordinate array stitches the holes into the exterior, then
    # callers passing geometry.holes subtract the same rings again.
    hole_rings = []
    if isinstance(boundary, shapely.Polygon):
        hole_rings.extend(boundary.interiors)
        boundary = shapely.get_exterior_ring(boundary)
    if holes is not None:
        hole_rings.extend(holes)
    boundary = shapely.polygons(
        shapely.get_coordinates(shapely.force_3d(boundary, z=0), include_z=True)
    )
    projection = Projection.fromPolygon(boundary)
    boundary = projection.toUV(boundary)
    boundary = simplify(boundary, include_z=True)
    boundary = shapely.get_coordinates(
        shapely.force_3d(boundary, z=0), include_z=True
    )[:-1]

    unique_holes = []
    for hole in hole_rings:
        ring = shapely.get_exterior_ring(hole) if isinstance(hole, shapely.Polygon) else hole
        projected = projection.toUV(shapely.polygons(ring))
        if not any(projected.equals(previous) for previous in unique_holes):
            unique_holes.append(projected)
    holes = [
        shapely.get_coordinates(shapely.force_3d(hole, z=0), include_z=True)[:-1]
        for hole in unique_holes
    ]

    convex_faces, divided_lines = GeometryConvexifier.convexify_faces_2d(
        [boundary],
        [holes],
        is_quad_clean=False,
    )
    convex_faces = [shapely.polygons(convex_face) for convex_face in convex_faces]
    convex_faces = [projection.toWorld(convex_face) for convex_face in convex_faces]
    divided_lines = [projection.toWorld(shapely.linestrings(line)) for line in divided_lines]
    return convex_faces, divided_lines
