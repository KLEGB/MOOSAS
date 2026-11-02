"""
Convexification utilities aligned with the current cuger implementation.
"""

from ...utils import np, shapely
from .polygon import GeometryBasic, GeometryOperator, GeometryValidator


def _convex_parts_with_holes(face, holes):
    """Triangulate a polygon with holes and merge adjacent triangles when convex."""
    face = np.asarray(face, dtype=float)
    polygon = shapely.polygons(
        face[:, :2],
        # Rings can have different vertex counts. Build each ring before
        # passing them to Shapely so NumPy does not coerce ragged coordinates.
        holes=[shapely.linearrings(np.asarray(hole, dtype=float)[:, :2]) for hole in holes],
    )
    # Bridged hole contours may repeat a hole already present in the ring.
    # Repair their topology before triangulation, retaining all area parts
    # while excluding the zero-area bridge lines from the triangulator.
    if not shapely.is_valid(polygon):
        polygon = shapely.make_valid(polygon)
    pieces = [
        part
        for region in shapely.get_parts(polygon)
        if shapely.get_dimensions(region) == 2
        for part in shapely.get_parts(shapely.constrained_delaunay_triangles(region))
        if shapely.area(part) > 1e-6
    ]

    merged = True
    while merged:
        merged = False
        for first_idx in range(len(pieces) - 1):
            for second_idx in range(first_idx + 1, len(pieces)):
                if shapely.length(
                    shapely.intersection(
                        shapely.boundary(pieces[first_idx]),
                        shapely.boundary(pieces[second_idx]),
                    )
                ) <= 1e-6:
                    continue
                union = shapely.union(pieces[first_idx], pieces[second_idx])
                if len(shapely.get_parts(union)) != 1:
                    continue
                if shapely.area(shapely.convex_hull(union)) - shapely.area(union) > 1e-6:
                    continue
                pieces[first_idx] = union
                pieces.pop(second_idx)
                merged = True
                break
            if merged:
                break

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

    print("--Faces reordering done--")

    for idx, face in enumerate(faces):
        if valid_face and not GeometryValidator._is_valid_face(face):
            print(f"    Skipping invalid face {idd[idx]}")
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
                        print(f"    Skipping invalid sub-face in face {idd[idx]}")
                        continue

                    if clean_quad and len(poly) > 4:
                        quad_poly = GeometryOperator.compute_max_inscribed_quadrilateral(candidate_face)
                        if valid_face and not GeometryValidator._is_valid_face(quad_poly):
                            print(f"    Skipping invalid quadrilateral sub-face in face {idd[idx]}")
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

    print("--Faces splitting done--")

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
