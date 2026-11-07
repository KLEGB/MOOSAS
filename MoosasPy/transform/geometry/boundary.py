'''Building-boundary simplification and window reconstruction.'''
from __future__ import annotations

from scipy.spatial.transform import Rotation as R

from ...utils import np, shapely
from .geos import Vector
from .polygon import GeometryBasic


def create_obb(points, normal, min_scale=0.1):
    """Create an oriented bounding box (OBB) for a set of points."""
    points = np.asarray(points, dtype=float)
    normal = np.asarray(normal, dtype=float)
    if len(points) == 0:
        raise ValueError("points must not be empty")
    if np.linalg.norm(normal) <= 1e-8:
        normal = np.array([0.0, 0.0, 1.0], dtype=float)
    else:
        normal = normal / np.linalg.norm(normal)

    geometry = shapely.multipoints(points)
    z_axis = np.array([0.0, 0.0, 1.0], dtype=float)
    z_r = normal

    if np.abs(z_r[0]) <= 1e-3 and np.abs(z_r[1]) <= 1e-3:
        z_r = z_axis
        min_rotated_rectangle = shapely.minimum_rotated_rectangle(geometry)
        obb_coords = np.array(
            shapely.get_coordinates(min_rotated_rectangle, include_z=True)
        )[:-1]
        obb_coords = np.nan_to_num(obb_coords, nan=points[0, 2])
        obb_coords[:, 2] = (np.min(points[:, 2]) + np.max(points[:, 2])) / 2

        if len(obb_coords) <= 2:
            centroid = np.mean(points, axis=0)
            x_r = np.array([1.0, 0.0, 0.0], dtype=float)
            y_r = np.array([0.0, 1.0, 0.0], dtype=float)
            rotation_matrix = R.from_matrix(np.array([x_r, y_r, z_r])).as_matrix()
            l = max(np.ptp(points[:, 0]), min_scale)
            w = max(np.ptp(points[:, 1]), min_scale)
            h = max(np.ptp(points[:, 2]), min_scale)
            original_obb_centroid = centroid
        else:
            x_vec = obb_coords[1] - obb_coords[0]
            x_norm = np.linalg.norm(x_vec)
            x_r = (
                x_vec / x_norm
                if x_norm > 1e-6
                else np.array([1.0, 0.0, 0.0], dtype=float)
            )
            y_r = np.cross(z_r, x_r)

            rotation_matrix = R.from_matrix(np.array([x_r, y_r, z_r])).as_matrix()

            l = max(np.linalg.norm(obb_coords[1] - obb_coords[0]), min_scale)
            w = max(np.linalg.norm(obb_coords[3] - obb_coords[0]), min_scale)
            h = max(np.max(points[:, 2]) - np.min(points[:, 2]), min_scale)
            original_obb_centroid = np.mean(obb_coords, axis=0)
    else:
        x_r = np.cross(z_r, z_axis)
        if np.linalg.norm(x_r) <= 1e-8:
            x_r = np.array([1.0, 0.0, 0.0], dtype=float)
        else:
            x_r = x_r / np.linalg.norm(x_r)

        y_r = np.cross(z_r, x_r)
        if np.linalg.norm(y_r) <= 1e-8:
            y_r = np.array([0.0, 1.0, 0.0], dtype=float)
        else:
            y_r = y_r / np.linalg.norm(y_r)

        rotation_matrix = R.from_matrix(np.array([x_r, y_r, z_r])).as_matrix()

        rotated_points = points.dot(rotation_matrix.T)
        l = max(np.ptp(rotated_points[:, 0]), min_scale)
        w = max(np.ptp(rotated_points[:, 1]), min_scale)
        h = max(np.ptp(rotated_points[:, 2]), min_scale)

        centroid = np.mean(
            [
                [np.min(rotated_points[:, 0]), np.min(rotated_points[:, 1]), np.min(rotated_points[:, 2])],
                [np.max(rotated_points[:, 0]), np.max(rotated_points[:, 1]), np.max(rotated_points[:, 2])],
            ],
            axis=0,
        )
        original_obb_centroid = np.dot(centroid, rotation_matrix)

    return {
        "center": original_obb_centroid,
        "scale": np.array([l, w, h], dtype=float),
        "rotation": rotation_matrix,
    }


def obb_to_face_vertices(obb_params):
    """Convert OBB parameters to face vertices and normals."""
    center = obb_params["center"]
    l, w, h = obb_params["scale"]
    rotation_matrix = obb_params["rotation"]

    offsets = np.array(
        [
            [-l / 2, -w / 2, -h / 2],
            [l / 2, -w / 2, -h / 2],
            [l / 2, w / 2, -h / 2],
            [-l / 2, w / 2, -h / 2],
            [-l / 2, -w / 2, h / 2],
            [l / 2, -w / 2, h / 2],
            [l / 2, w / 2, h / 2],
            [-l / 2, w / 2, h / 2],
        ]
    )

    corners = offsets.dot(rotation_matrix) + center
    faces = [
        [corners[0], corners[1], corners[2], corners[3]],
        [corners[4], corners[7], corners[6], corners[5]],
        [corners[0], corners[4], corners[5], corners[1]],
        [corners[2], corners[6], corners[7], corners[3]],
        [corners[0], corners[3], corners[7], corners[4]],
        [corners[1], corners[5], corners[6], corners[2]],
    ]

    faces = [np.array(face) for face in faces]
    normals = []
    for face in faces:
        v1 = face[1] - face[0]
        v2 = face[3] - face[0]
        normal = np.cross(v1, v2)
        normal = np.round(normal / (np.linalg.norm(normal) + 1e-6), 3)
        normals.append(normal)

    return faces, normals


def calculate_wwr(cats, faces, normals):
    """Calculate the window-to-wall ratio."""
    total_wall_area = 0.0
    total_window_area = 0.0

    for cat, face, normal in zip(cats, faces, normals):
        area = GeometryBasic.polygon_area_3d(face)
        if area < 1e-6:
            continue

        if abs(normal[2]) < 1e-3:
            if int(cat) in (1, 5):
                total_window_area += area
            else:
                total_wall_area += area

    if total_wall_area + total_window_area == 0:
        return 0.0

    return total_window_area / (total_wall_area + total_window_area)


def geometry_arrays(geometries):
    categories, face_ids, normals, faces, holes = [], [], [], [], []
    for geometry in geometries:
        rings = shapely.get_rings(geometry.face)
        categories.append(geometry.category)
        face_ids.append(geometry.faceId)
        normals.append(Vector(geometry.normal).array)
        faces.append(shapely.get_coordinates(rings[0], include_z=True)[:-1])
        face_holes = {
            index: shapely.get_coordinates(ring, include_z=True)[:-1]
            for index, ring in enumerate(rings[1:])
        }
        holes.append(face_holes or None)
    return categories, face_ids, normals, faces, holes


def _create_window_on_wall(face, normal, wwr, margin_ratio=0.1):
    if wwr <= 0 or abs(normal[2]) > 0.7:
        return None, None
    center = np.mean(face, axis=0)
    horizontal = face[1] - face[0]
    vertical = face[3] - face[0]
    horizontal_unit = horizontal / (np.linalg.norm(horizontal) + 1e-10)
    vertical_unit = vertical / (np.linalg.norm(vertical) + 1e-10)
    wall_to_window_ratio = wwr / (1 - wwr)
    scale = np.sqrt(wall_to_window_ratio)
    half_width = np.linalg.norm(horizontal) * scale / 2
    half_height = np.linalg.norm(vertical) * scale / 2
    window = np.array([
        center - half_width * horizontal_unit - half_height * vertical_unit,
        center + half_width * horizontal_unit - half_height * vertical_unit,
        center + half_width * horizontal_unit + half_height * vertical_unit,
        center - half_width * horizontal_unit + half_height * vertical_unit,
    ])
    return {0: window}, window


def simplify_to_layered_obb(categories, normals, faces):
    if not faces:
        raise ValueError('Boundary simplification requires non-empty geometry.')
    min_area = 1e-5
    horizontal_faces = [
        np.asarray(face, dtype=float)
        for face, normal in zip(faces, normals)
        if abs(normal[2]) > 0.7
        and len(face) >= 3
        and GeometryBasic.polygon_area_3d(face) > min_area
    ]
    if len(horizontal_faces) < 2:
        raise ValueError('Boundary simplification requires at least two horizontal levels.')

    z_centers = [float(np.mean(face[:, 2])) for face in horizontal_faces]
    z_tolerance = max(0.05, (max(z_centers) - min(z_centers)) * 0.01)
    level_groups = []
    for z_center, face in sorted(zip(z_centers, horizontal_faces), key=lambda item: item[0]):
        if not level_groups or abs(z_center - np.mean(level_groups[-1]['z_values'])) > z_tolerance:
            level_groups.append({'z_values': [z_center], 'faces': [face]})
        else:
            level_groups[-1]['z_values'].append(z_center)
            level_groups[-1]['faces'].append(face)
    levels = [float(np.mean(group['z_values'])) for group in level_groups]
    if len(levels) < 2:
        raise ValueError('Boundary simplification requires two distinct horizontal levels.')

    wall_faces = [
        np.asarray(face, dtype=float)
        for face, normal in zip(faces, normals)
        if abs(normal[2]) <= 0.7
        and len(face) >= 3
        and GeometryBasic.polygon_area_3d(face) > min_area
    ]
    if not wall_faces:
        raise ValueError('Boundary simplification requires vertical boundary faces.')

    wwr = calculate_wwr(categories, faces, normals)
    out_categories, out_ids, out_normals, out_faces, out_holes = [], [], [], [], []
    for layer_index, (bottom_z, top_z) in enumerate(zip(levels[:-1], levels[1:]), start=1):
        layer_height = top_z - bottom_z
        if layer_height <= 1e-3:
            raise ValueError(f'Boundary simplification found a degenerate layer {layer_index}.')
        layer_tolerance = max(1e-4, layer_height * 0.02)
        layer_walls = [
            wall for wall in wall_faces
            if float(np.min(wall[:, 2])) <= top_z + layer_tolerance
            and float(np.max(wall[:, 2])) >= bottom_z - layer_tolerance
        ]
        if not layer_walls:
            raise ValueError(f'Boundary simplification found no walls for layer {layer_index}.')

        vertices = np.vstack(layer_walls)
        if np.ptp(vertices[:, 0]) <= 1e-4 or np.ptp(vertices[:, 1]) <= 1e-4:
            raise ValueError(f'Boundary simplification found a degenerate layer {layer_index}.')

        obb = create_obb(vertices, (0, 0, 1))
        obb['scale'][2] = layer_height
        obb['center'][2] = bottom_z + layer_height / 2
        obb_faces, obb_normals = obb_to_face_vertices(obb)
        for face_index, (face, normal) in enumerate(zip(obb_faces, obb_normals)):
            if GeometryBasic.polygon_area_3d(face) <= min_area:
                raise ValueError(
                    f'Boundary simplification generated a degenerate layer {layer_index}.'
                )
            hole, window = _create_window_on_wall(face, normal, wwr)
            out_categories.append(0)
            out_ids.append(f'layer{layer_index}_obb_face_{face_index}')
            out_normals.append(normal)
            out_faces.append(face)
            out_holes.append(hole)
            if window is not None:
                out_categories.append(1)
                out_ids.append(f'layer{layer_index}_obb_window_{face_index}')
                out_normals.append(normal)
                out_faces.append(window)
                out_holes.append(None)

    return np.asarray(out_categories), out_ids, np.asarray(out_normals), out_faces, out_holes
