"""
Convexification utilities aligned with the current cuger implementation.
"""

from ...utils import GeometryError, np, shapely
from . import polygon as polygon_ops
from .geos import Projection, simplify
from .polygon import GeometryBasic, GeometryOperator, GeometryValidator


def convexify_shape(boundary: shapely.Geometry, holes: list[shapely.Geometry] | None = None):
    """Convert a planar shape to coordinate arrays and return convex shapes and cuts."""
    if holes is None:
        holes = list(shapely.get_rings(boundary))[1:] if shapely.get_dimensions(boundary) == 2 else []
    shell = shapely.get_exterior_ring(boundary) if shapely.get_dimensions(boundary) == 2 else boundary
    boundary = shapely.polygons(
        shapely.get_coordinates(shapely.force_3d(shell, z=0), include_z=True)
    )
    projection = Projection.fromPolygon(boundary)
    boundary = projection.toUV(boundary)
    boundary = simplify(boundary, include_z=True)
    coordinates = shapely.get_coordinates(
        shapely.force_3d(boundary, z=0), include_z=True
    )[:-1]

    projected_holes = [projection.toUV(hole) for hole in holes]
    hole_coordinates = [
        shapely.get_coordinates(shapely.force_3d(hole, z=0), include_z=True)[:-1]
        for hole in projected_holes
    ]

    convex_faces, divided_lines = GeometryConvexifier.convexify_faces_2d(
        [coordinates],
        [hole_coordinates],
        is_quad_clean=False,
    )
    convex_faces = [shapely.polygons(convex_face) for convex_face in convex_faces]
    convex_faces = [projection.toWorld(convex_face) for convex_face in convex_faces]
    divided_lines = [projection.toWorld(shapely.linestrings(line)) for line in divided_lines]
    return convex_faces, divided_lines


def _convex_parts_with_holes(face, holes):
    """Remove holes using double cuts, then convexify the resulting simple faces."""
    face = np.asarray(face, dtype=float)
    polygon = shapely.Polygon(
        face[:, :2],
        # Rings can have different vertex counts. Build each ring before
        # passing them to Shapely so NumPy does not coerce ragged coordinates.
        holes=[shapely.linearrings(np.asarray(hole, dtype=float)[:, :2]) for hole in holes],
    )
    if not polygon.is_valid or polygon.is_empty or polygon.area <= 0:
        raise GeometryError(polygon, "Double-cut decomposition requires a valid polygon with holes.")

    pending = [polygon]
    simple = []
    while pending:
        piece = pending.pop()
        if piece.interiors:
            pending.extend(polygon_ops.cut_hole(piece))
        else:
            simple.append(piece)
    polygon_ops._validate_partition(simple, polygon)

    pieces = []
    for piece in simple:
        vertices = GeometryOperator.reorder_vertices(
            np.asarray(piece.exterior.coords)[:-1], is_upward=True
        )
        faces, _ = GeometryOperator.split_poly(vertices, list(range(len(vertices))))
        subparts = [shapely.Polygon(vertices[indices]) for indices in faces]
        polygon_ops._validate_partition(subparts, piece, convex=True)
        pieces.extend(subparts)
    polygon_ops._validate_partition(pieces, polygon, convex=True)
    z_value = float(np.mean(face[:, 2])) if face.shape[1] > 2 else 0.0
    result = []
    for piece in pieces:
        coordinates = shapely.get_coordinates(piece)[:-1]
        result.append(np.column_stack((coordinates, np.full(len(coordinates), z_value))))
    return result


def convexify_faces(cat, idd, normal, faces, holes, valid_face=True, clean_quad=False):
    """Convexify polygonal faces and generate quadrilateral air-wall patches."""
    convex_cat = []
    convex_idd = []
    convex_normal = []
    convex_faces = []
    divide_lines = []

    for idx in range(len(faces)):
        if np.abs(normal[idx][2]) > 1e-3:
            faces[idx] = GeometryOperator.reorder_vertices(faces[idx], is_upward=True)
            if holes[idx]:
                for i in range(len(holes[idx])):
                    holes[idx][i] = GeometryOperator.reorder_vertices(holes[idx][i], is_upward=False)

    print("CONVEXIFY: Faces reordering done")

    for idx, face in enumerate(faces):
        if valid_face and not GeometryValidator._is_valid_face(face):
            print(f"******Warning: CONVEXIFY: Skipping invalid face {idd[idx]}")
            continue

        if np.abs(normal[idx][2]) > 1e-3:
            poly_ex = face

            poly_in = {}
            if holes[idx]:
                for i in range(len(holes[idx])):
                    hole = holes[idx][i]
                    should_skip = GeometryOperator.process_hole(hole, faces, check_projection=True)
                    if should_skip:
                        continue
                    poly_in[i] = hole

            if poly_in:
                subfaces = _convex_parts_with_holes(poly_ex, list(poly_in.values()))
                diags = []
            else:
                verts = poly_ex
                indices = list(range(len(verts)))
                polys, diags = GeometryOperator.split_poly(verts, indices)

                subfaces = []
                for poly in polys:
                    candidate_face = verts[poly]

                    if valid_face and not GeometryValidator._is_valid_face(candidate_face):
                        print(f"******Warning: CONVEXIFY: Skipping invalid sub-face in face {idd[idx]}")
                        continue

                    if clean_quad and len(poly) > 4:
                        quad_poly = GeometryOperator.compute_max_inscribed_quadrilateral(candidate_face)
                        if valid_face and not GeometryValidator._is_valid_face(quad_poly):
                            print(f"******Warning: CONVEXIFY: Skipping invalid quadrilateral sub-face in face {idd[idx]}")
                            continue
                        candidate_face = np.array(quad_poly)

                    subfaces.append(candidate_face)

            if len(subfaces) == 1:
                for subface in subfaces:
                    convex_cat.append(cat[idx])
                    convex_idd.append(idd[idx])
                    convex_normal.append(normal[idx])
                    convex_faces.append(subface)
            else:
                for i, subface in enumerate(subfaces):
                    convex_cat.append(cat[idx])
                    convex_idd.append(f"#{idd[idx]}_{i}")
                    convex_normal.append(normal[idx])
                    convex_faces.append(subface)

                if diags:
                    sublines = [np.array([verts[pair[0]], verts[pair[1]]]) for pair in diags]
                    divide_lines.extend(sublines)
        else:
            convex_cat.append(cat[idx])
            convex_idd.append(idd[idx])
            convex_normal.append(normal[idx])
            convex_faces.append(face)

    print("CONVEXIFY: Faces splitting done")

    return convex_cat, convex_idd, convex_normal, convex_faces, divide_lines


class GeometryConvexifier:
    """Convexification operations for polygonal building geometry."""

    @staticmethod
    def convexify_faces(cat, idd, normal, faces, holes, is_valid_face=True, is_quad_clean=False):
        return convexify_faces(
            cat,
            idd,
            normal,
            faces,
            holes,
            valid_face=is_valid_face,
            clean_quad=is_quad_clean,
        )

    @staticmethod
    def convexify_faces_2d(faces, holes, is_valid_face=True, is_quad_clean=False):
        convex_faces = []
        divide_lines = []

        for idx, face in enumerate(faces):
            face = GeometryOperator.reorder_vertices(face, is_upward=True)
            hole_dict = {}

            if holes[idx]:
                for i, hole in enumerate(holes[idx]):
                    hole = GeometryOperator.reorder_vertices(hole, is_upward=False)
                    should_skip = GeometryOperator.process_hole(hole, faces, check_projection=True)
                    if should_skip:
                        continue
                    hole_dict[i] = hole

            if hole_dict:
                convex_faces.extend(_convex_parts_with_holes(face, list(hole_dict.values())))
                continue
            else:
                verts = face

            indices = list(range(len(verts)))
            polys, diags = GeometryOperator.split_poly(verts, indices)

            for poly in polys:
                candidate_face = verts[poly]
                if is_valid_face and not GeometryValidator._is_valid_face(candidate_face):
                    continue

                if is_quad_clean and len(poly) > 4:
                    quad_poly = GeometryOperator.compute_max_inscribed_quadrilateral(candidate_face)
                    if is_valid_face and not GeometryValidator._is_valid_face(quad_poly):
                        continue
                    candidate_face = np.array(quad_poly)

                convex_faces.append(candidate_face)

            if diags:
                divide_lines.extend([np.array([verts[pair[0]], verts[pair[1]]]) for pair in diags])

        return convex_faces, divide_lines
