"""
Geometry utilities for convexification and simplification in Moosas.
"""

from typing import List, Tuple, Union

from ...utils import np, shapely


def planar_surface(geometry):
    """Preserve planar area while removing invalid nonphysical linework."""
    region = shapely.force_2d(geometry)
    if not region.is_valid:
        stack = [shapely.make_valid(region)]
        polygons = []
        while stack:
            part = stack.pop()
            kind = shapely.get_type_id(part)
            if kind == 3:
                polygons.append(part)
            elif kind in (6, 7):
                stack.extend(shapely.get_parts(part))
        repaired = shapely.union_all(polygons)
        if abs(repaired.area - region.area) > max(1e-8, abs(region.area) * 1e-8):
            raise ValueError('Planar validity repair changes area; source geometry requires review')
        region = repaired
    # Drop precision metadata without rounding physical coordinates.
    return shapely.set_precision(region, 0)


class GeometryBasic:
    """Basic geometry utility methods."""

    @staticmethod
    def angle(p1, p2, p3):
        """Calculate the signed angle at ``p2`` formed by ``p1-p2-p3``."""
        v1 = p2 - p1
        v2 = p3 - p2
        cross = np.cross(v1, v2)
        dot = np.dot(v1, v2)

        if np.linalg.norm(cross) < 1e-3 * np.linalg.norm(v1) * np.linalg.norm(v2):
            return 0

        angle_rad = np.arccos(np.clip(dot / (np.linalg.norm(v1) * np.linalg.norm(v2)), -1.0, 1.0))
        angle_deg = np.degrees(angle_rad)
        return angle_deg if cross[2] > 0 else -angle_deg

    @staticmethod
    def get_angle_tan(p1, p2, verts_all):
        vec = verts_all[p2] - verts_all[p1]
        return np.arctan2(vec[1], vec[0])

    @staticmethod
    def polygon_area_2d(vertices):
        """Calculate 2D polygon area by the shoelace formula."""
        if len(vertices) < 3:
            return 0.0

        area = 0.0
        n = len(vertices)
        for i in range(n):
            x1, y1 = vertices[i][:2]
            x2, y2 = vertices[(i + 1) % n][:2]
            area += x1 * y2 - x2 * y1

        return abs(area) / 2.0

    @staticmethod
    def polygon_area_3d(vertices):
        """Calculate 3D polygon area using Newell's method."""
        if len(vertices) < 3:
            return 0.0

        v = np.asarray(vertices, dtype=float)
        normal = np.zeros(3)
        for i in range(len(v)):
            current = v[i]
            next_vertex = v[(i + 1) % len(v)]
            normal[0] += (current[1] - next_vertex[1]) * (current[2] + next_vertex[2])
            normal[1] += (current[2] - next_vertex[2]) * (current[0] + next_vertex[0])
            normal[2] += (current[0] - next_vertex[0]) * (current[1] + next_vertex[1])

        return np.linalg.norm(normal) / 2.0


class GeometryValidator:
    """Geometry validation helpers."""

    @staticmethod
    def _orientation(p1, p2, p3) -> int:
        """Return the robust XY turn sign without a geometric snapping tolerance."""
        ring = shapely.LinearRing([p1[:2], p2[:2], p3[:2]])
        if shapely.is_ccw(ring):
            return 1
        if shapely.is_ccw(shapely.reverse(ring)):
            return -1
        return 0

    @staticmethod
    def _is_left_on(p1, p2, p3):
        return GeometryValidator._orientation(p1, p2, p3) >= 0

    @staticmethod
    def _is_collinear(p1, p2, p3):
        return GeometryValidator._orientation(p1, p2, p3) == 0

    @staticmethod
    def _is_between(p1, p2, p3):
        return GeometryValidator._is_collinear(p1, p2, p3) and all(
            min(p1[axis], p2[axis]) <= p3[axis] <= max(p1[axis], p2[axis])
            for axis in range(2)
        )

    @staticmethod
    def _is_intersect(a, b, c, d):
        """Include endpoint contacts, collinear overlaps and point segments."""
        abc = GeometryValidator._orientation(a, b, c)
        abd = GeometryValidator._orientation(a, b, d)
        cda = GeometryValidator._orientation(c, d, a)
        cdb = GeometryValidator._orientation(c, d, b)
        return (
            (abc * abd < 0 and cda * cdb < 0)
            or (abc == 0 and GeometryValidator._is_between(a, b, c))
            or (abd == 0 and GeometryValidator._is_between(a, b, d))
            or (cda == 0 and GeometryValidator._is_between(c, d, a))
            or (cdb == 0 and GeometryValidator._is_between(c, d, b))
        )

    @staticmethod
    def _is_obtuse(v1, v2, v3):
        return GeometryBasic.angle(v1, v2, v3) > 90

    @staticmethod
    def _is_valid_face(vertices, area_eps=1e-8):
        if vertices is None or len(vertices) < 3:
            return False

        v = np.asarray(vertices, dtype=float)
        if not np.isfinite(v).all():
            return False

        p0 = v[0]
        area = 0.0
        for i in range(1, len(v) - 1):
            e1 = v[i] - p0
            e2 = v[i + 1] - p0
            area += 0.5 * np.linalg.norm(np.cross(e1, e2))

        return area > area_eps

    @staticmethod
    def _is_same_polygon(polygon1, polygon2, projection=False):
        if polygon1.shape != polygon2.shape:
            return False

        if projection:
            if polygon1.shape[1] < 2 or polygon2.shape[1] < 2:
                return False
            poly1 = polygon1[:, :2]
            poly2 = polygon2[:, :2]
        else:
            poly1 = polygon1
            poly2 = polygon2

        if np.array_equal(poly1, poly2):
            return True

        if np.array_equal(poly1[0], poly2[0]) and np.array_equal(poly1[1:], poly2[1:][::-1]):
            return True

        return False

    @staticmethod
    def _is_diagonal(verts: np.ndarray, indices: np.ndarray, ia: int, ib: int) -> bool:
        """Test a strictly internal diagonal of a counterclockwise XY polygon."""
        n = len(indices)
        if ia == ib or (ia - ib) % n in (1, n - 1):
            return False
        if np.array_equal(verts[indices[ia]][:2], verts[indices[ib]][:2]):
            return False

        def in_cone(local_verts: np.ndarray, local_indices: np.ndarray, idx_a: int, idx_b: int) -> bool:
            n = len(local_indices)
            ia_prev = idx_a - 1 if idx_a - 1 >= 0 else n - 1
            ia_next = idx_a + 1 if idx_a + 1 < n else 0

            idx_a, idx_b = local_indices[idx_a], local_indices[idx_b]
            ia_prev, ia_next = local_indices[ia_prev], local_indices[ia_next]

            if GeometryValidator._is_left_on(local_verts[ia_prev], local_verts[idx_a], local_verts[ia_next]):
                return (
                    GeometryValidator._orientation(local_verts[idx_a], local_verts[idx_b], local_verts[ia_prev]) > 0
                    and GeometryValidator._orientation(local_verts[idx_b], local_verts[idx_a], local_verts[ia_next]) > 0
                )
            return not (
                GeometryValidator._is_left_on(local_verts[idx_a], local_verts[idx_b], local_verts[ia_next]) and
                GeometryValidator._is_left_on(local_verts[idx_b], local_verts[idx_a], local_verts[ia_prev])
            )

        def diagonalie(local_verts: np.ndarray, local_indices: np.ndarray, idx_a: int, idx_b: int) -> bool:
            n = len(local_indices)
            for now_i in range(n):
                if local_indices[now_i] == local_indices[idx_a] or local_indices[now_i] == local_indices[idx_b]:
                    continue
                next_i = (now_i + 1) % n
                if local_indices[next_i] == local_indices[idx_a] or local_indices[next_i] == local_indices[idx_b]:
                    continue

                if GeometryValidator._is_intersect(
                    local_verts[local_indices[idx_a]],
                    local_verts[local_indices[idx_b]],
                    local_verts[local_indices[now_i]],
                    local_verts[local_indices[next_i]],
                ):
                    return False
            return True

        return in_cone(verts, indices, ia, ib) and in_cone(verts, indices, ib, ia) and diagonalie(verts, indices, ia, ib)


class GeometryOperator:
    """Geometry transformation and polygon decomposition helpers."""

    @staticmethod
    def reorder_vertices(face, is_upward):
        face = np.asarray(face, dtype=float)
        if len(face) <= 1:
            return face

        min_index = np.argmin(np.sum(face, axis=1))
        face = np.roll(face, -min_index, axis=0)

        x = face[:, 0]
        y = face[:, 1]
        signed_area = 0.5 * np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y)
        is_ccw = signed_area > 0

        if (is_upward and not is_ccw) or ((not is_upward) and is_ccw):
            face = face[::-1]

        min_index = np.argmin(np.sum(face, axis=1))
        face = np.roll(face, -min_index, axis=0)
        return face

    @staticmethod
    def compute_max_inscribed_quadrilateral(vertices):
        if len(vertices) <= 4:
            return vertices

        max_area = 0.0
        best_quad_indices = None
        for i in range(len(vertices)):
            for j in range(i + 1, len(vertices)):
                for k in range(j + 1, len(vertices)):
                    for l in range(k + 1, len(vertices)):
                        quad_vertices = [vertices[i], vertices[j], vertices[k], vertices[l]]
                        area = GeometryBasic.polygon_area_2d(quad_vertices)
                        if area > max_area:
                            max_area = area
                            best_quad_indices = [i, j, k, l]

        if best_quad_indices is not None:
            return [vertices[idx] for idx in best_quad_indices]
        return vertices

    @staticmethod
    def process_hole(hole, faces, check_projection=True):
        for other_face in faces:
            if GeometryValidator._is_same_polygon(hole, other_face):
                if not check_projection:
                    return True

                has_projection_overlap = False
                for face in faces:
                    if GeometryValidator._is_same_polygon(hole, face):
                        continue
                    if GeometryValidator._is_same_polygon(hole, face, projection=True):
                        has_projection_overlap = True
                        break

                if not has_projection_overlap:
                    return True

        return False

    @staticmethod
    def merge_holes(verts_poly: np.ndarray, verts_holes: dict[int, np.ndarray]) -> np.ndarray:
        if verts_holes is None or len(verts_holes) == 0:
            return verts_poly, []

        n_poly = len(verts_poly)
        indices_poly = list(range(n_poly))
        indices_holes = {}
        verts_all = verts_poly.copy()

        offset = n_poly
        for hole_id, verts_hole in verts_holes.items():
            n_hole = len(verts_hole)
            indices_holes[hole_id] = list(range(offset, offset + n_hole))
            verts_all = np.concatenate((verts_all, verts_hole))
            offset += n_hole

        best_diagonals = {}

        for hole_id, indices_hole in indices_holes.items():
            verts_hole = verts_holes[hole_id]
            n_hole = len(indices_hole)
            min_diagonal_length = float("inf")
            min_diagonal = None

            for hole_idx, hole_vert_idx in enumerate(indices_hole):
                hole_vertex = verts_hole[hole_idx]
                for poly_idx, poly_vertex_idx in enumerate(indices_poly):
                    poly_vertex = verts_poly[poly_idx]
                    okay = True

                    for poly_edge in range(n_poly):
                        poly_a = verts_poly[poly_edge]
                        poly_b = verts_poly[(poly_edge + 1) % n_poly]
                        if poly_idx in (poly_edge, (poly_edge + 1) % n_poly):
                            continue
                        if GeometryValidator._is_intersect(poly_vertex, hole_vertex, poly_a, poly_b):
                            okay = False
                            break

                    if not okay:
                        continue

                    for hole_edge in range(n_hole):
                        hole_a = verts_hole[hole_edge]
                        hole_b = verts_hole[(hole_edge + 1) % n_hole]
                        if hole_idx in (hole_edge, (hole_edge + 1) % n_hole):
                            continue
                        if GeometryValidator._is_intersect(poly_vertex, hole_vertex, hole_a, hole_b):
                            okay = False
                            break

                    if not okay:
                        continue

                    for other_id, other_indices in indices_holes.items():
                        if other_id == hole_id:
                            continue
                        other_verts = verts_holes[other_id]
                        for edge in range(len(other_verts)):
                            a = other_verts[edge]
                            b = other_verts[(edge + 1) % len(other_verts)]
                            if GeometryValidator._is_intersect(poly_vertex, hole_vertex, a, b):
                                okay = False
                                break
                        if not okay:
                            break

                    if okay:
                        diagonal_length = np.linalg.norm(poly_vertex - hole_vertex)
                        if diagonal_length < min_diagonal_length:
                            min_diagonal_length = diagonal_length
                            min_diagonal = (poly_vertex_idx, hole_vert_idx)

            if min_diagonal is not None:
                best_diagonals[hole_id] = min_diagonal

        diagonals = list(best_diagonals.values())
        diagonals = sorted(diagonals, key=lambda x: (x[0], -GeometryBasic.get_angle_tan(x[0], x[1], verts_all)))

        verts = []
        for idx in indices_poly:
            verts.append(verts_all[idx])

            for diagonal in diagonals:
                if diagonal[0] != idx:
                    continue

                hole_vertex = diagonal[1]
                target_hole_indices = None
                for hole_id, hole_indices in indices_holes.items():
                    if hole_vertex in hole_indices:
                        target_hole_indices = hole_indices
                        break

                if target_hole_indices:
                    start_idx = target_hole_indices.index(hole_vertex)
                    n_hole = len(target_hole_indices)
                    for i in range(n_hole + 1):
                        current_idx = target_hole_indices[(start_idx + i) % n_hole]
                        verts.append(verts_all[current_idx])
                    verts.append(verts_all[idx])

        mergelines = [np.array([verts_all[pair[0]], verts_all[pair[1]]]) for pair in diagonals]
        return np.array(verts), mergelines

    @staticmethod
    def split_poly(verts: np.ndarray, indices: np.ndarray) -> Union[List[np.ndarray], List[Tuple[int, int]]]:
        n = len(indices)
        i_concave = -1

        for ia in range(n):
            ia_prev, ia_next = (ia - 1) % n, (ia + 1) % n
            orientation = GeometryValidator._orientation(
                verts[indices[ia_prev]], verts[indices[ia]], verts[indices[ia_next]],
            )
            if orientation < 0:
                i_concave = ia
                break

        if i_concave == -1:
            return [indices], []

        i_break = -1
        min_diagonal_length = float("inf")
        for i in range(n):
            if i != i_concave and i != (i_concave + 1) % n and i != (i_concave - 1) % n:
                if GeometryValidator._is_diagonal(verts, indices, i_concave, i):
                    diagonal_length = np.linalg.norm(verts[indices[i_concave]] - verts[indices[i]])
                    if diagonal_length < min_diagonal_length:
                        i_break = i
                        min_diagonal_length = diagonal_length

        if i_break == -1:
            return [indices], []

        indices1 = []
        indices2 = []
        i_now = i_concave

        while i_now != i_break:
            indices1.append(indices[i_now])
            i_now = (i_now + 1) % n
        indices1.append(indices[i_break])

        while i_now != i_concave:
            indices2.append(indices[i_now])
            i_now = (i_now + 1) % n
        indices2.append(indices[i_concave])

        i1, diag1 = GeometryOperator.split_poly(verts, indices1)
        i2, diag2 = GeometryOperator.split_poly(verts, indices2)

        ret_diag = [[i_concave, i_break]]
        for diag in diag1:
            ret_diag.append(((diag[0] + i_concave) % n, (diag[1] + i_concave) % n))
        for diag in diag2:
            ret_diag.append(((diag[0] + i_break) % n, (diag[1] + i_break) % n))

        return i1 + i2, ret_diag
