"""Offset thermal zoning with original-length, zero-width hole constraints."""
from __future__ import annotations

import networkx as nx
from scipy.spatial import Voronoi
from shapely.geometry.polygon import orient

from ...utils import GeometryError, np, shapely
from ...utils.constant import geom
from .convexify import _validate_partition, _convex_parts_with_holes


_GRID = 1e-9
_SAMPLE_STEP = 0.1
_LINE_TOLERANCE = 1e-7


def _polygons(geometry):
    return [part for part in shapely.get_parts(geometry) if part.geom_type == "Polygon"]


def _lines(geometry):
    if geometry.is_empty:
        return []
    if geometry.geom_type == "LineString":
        return [geometry]
    if geometry.geom_type in ("MultiLineString", "GeometryCollection"):
        return [line for part in shapely.get_parts(geometry) for line in _lines(part)]
    return []


def _medial_lines(polygon):
    polygon = orient(polygon)
    points, labels, rings = [], [], []
    for ring_id, ring in enumerate([polygon.exterior, *polygon.interiors]):
        xy = np.asarray(ring.coords)[:-1, :2]
        rings.append(xy)
        for edge, (a, b) in enumerate(zip(xy, np.roll(xy, -1, axis=0))):
            count = max(1, int(np.ceil(np.linalg.norm(b - a) / _SAMPLE_STEP)))
            for fraction in np.arange(count) / count:
                points.append(a + fraction * (b - a))
                labels.append((ring_id, edge))
    diagram = Voronoi(np.asarray(points))
    result = []
    for sites, indices in zip(diagram.ridge_points, diagram.ridge_vertices):
        if len(indices) != 2 or -1 in indices:
            continue
        (ring_a, edge_a), (ring_b, edge_b) = [labels[i] for i in sites]
        if ring_a == ring_b:
            xy = rings[ring_a]
            if edge_a == edge_b:
                continue
            if (edge_a + 1) % len(xy) == edge_b:
                vertex = edge_b
            elif (edge_b + 1) % len(xy) == edge_a:
                vertex = edge_a
            else:
                vertex = None
            if vertex is not None:
                a = xy[vertex] - xy[vertex - 1]
                b = xy[(vertex + 1) % len(xy)] - xy[vertex]
                # Convex-corner spokes are not longitudinal strip centerlines.
                if a[0] * b[1] - a[1] * b[0] > 0:
                    continue
        line = shapely.LineString(diagram.vertices[indices])
        if line.length > _GRID and polygon.covers(line):
            result.append(line)
    # Voronoi vertices on parallel sides may differ by nanometers after rotation.
    return shapely.union_all(
        shapely.get_parts(shapely.set_precision(shapely.union_all(result), 1e-7)),
        grid_size=1e-7,
    )


def _collapse_narrow_core(source, core, minimum_width):
    # GEOS drops nearly collapsed buffers; retain the threshold within 0.2mm.
    radius = (minimum_width - 2e-4) / 2
    wide = shapely.union_all([
        part.buffer(-radius, join_style="mitre").buffer(radius, join_style="mitre")
        for part in _polygons(core)
    ]).intersection(core)
    wide = wide.simplify(0.005, preserve_topology=True)
    wide = shapely.set_precision(shapely.snap(wide, core, 1e-6).intersection(source), _GRID)
    removed = shapely.union_all(_polygons(
        shapely.set_precision(core.difference(wide), _GRID)
    ))
    if removed.area <= 1e-7:
        return wide, []
    skeleton = shapely.union_all([
        _medial_lines(part) for part in _polygons(core)
        if part.intersection(removed).area > 1e-7
    ])
    network = shapely.set_precision(skeleton.intersection(removed), _GRID)
    merged = _lines(shapely.line_merge(network))
    if not merged:
        raise GeometryError(core, "Narrow core has no usable medial axis.")
    degree = {}
    for line in _lines(network):
        for point in (line.coords[0], line.coords[-1]):
            key = tuple(np.round(point, 9))
            degree[key] = degree.get(key, 0) + 1
    reach = np.linalg.norm(np.subtract(removed.bounds[2:], removed.bounds[:2])) * 2
    extensions = []
    for line in merged:
        xy = np.asarray(line.coords)
        for point, inward in [(xy[0], xy[1]), (xy[-1], xy[-2])]:
            if degree[tuple(np.round(point, 9))] != 1:
                continue
            if removed.boundary.distance(shapely.Point(point)) <= _GRID * 5:
                continue
            direction = point - inward
            direction /= np.linalg.norm(direction)
            ray = shapely.LineString([point, point + reach * direction])
            coordinates = shapely.get_coordinates(ray.intersection(removed.boundary))
            distances = np.linalg.norm(coordinates - point, axis=1)
            eligible = distances > _GRID * 10
            if not eligible.any():
                raise GeometryError(core, "Cannot locate original narrow-region end.")
            target = coordinates[eligible][np.argmin(distances[eligible])]
            extensions.append(shapely.LineString([point, target]))
    network = shapely.set_precision(
        shapely.union_all([network, *extensions]).intersection(removed), _GRID
    )
    cuts = [
        segment for line in _lines(shapely.line_merge(network))
        for segment in _lines(line.simplify(
            _SAMPLE_STEP / 2, preserve_topology=True
        ).intersection(removed))
        if segment.length > _GRID * 10
    ]
    return wide, cuts


def validate_partition_lines(parts, cuts):
    """Require each atomic slit interval to border exactly two distinct faces."""
    if not cuts:
        return
    boundaries = [part.boundary for part in parts]
    union = shapely.union_all(boundaries)
    points = shapely.points(shapely.get_coordinates(union))
    for line in cuts:
        tolerance = max(1e-5, line.length * 1e-6)
        if line.difference(union.buffer(_LINE_TOLERANCE)).length > tolerance:
            raise GeometryError(line, "Zero-width hole absent from partition boundaries.")
        parameters = np.unique([
            0.0, line.length,
            *[line.project(shapely.Point(point)) for point in line.coords],
            *shapely.line_locate_point(
                line, points[shapely.distance(line, points) < _LINE_TOLERANCE]
            ),
        ])
        for start, end in zip(parameters, parameters[1:]):
            if end - start <= _GRID * 10:
                continue
            midpoint = line.interpolate((start + end) / 2)
            if sum(boundary.distance(midpoint) < _LINE_TOLERANCE
                   for boundary in boundaries) != 2:
                raise GeometryError(line, "Zero-width hole must have two adjacent faces.")
        if any(line.intersection(part.buffer(-_LINE_TOLERANCE)).length > tolerance
               for part in parts):
            raise GeometryError(line, "Partition crosses a zero-width hole.")


def _partition_cells(source, wide, cuts):
    network = shapely.union_all([source.boundary, wide.boundary, *cuts], grid_size=_GRID)
    constraints = shapely.union_all(cuts)
    visible_region = source.difference(wide).buffer(_GRID * 10)

    def measure(network):
        cells = [
            cell for cell in _polygons(shapely.polygonize(shapely.get_parts(network)))
            if source.covers(cell.representative_point())
        ]
        boundary = shapely.union_all([cell.boundary for cell in cells])
        unused = constraints.difference(boundary.buffer(_LINE_TOLERANCE)).length
        graph = nx.Graph()
        for line in _lines(network):
            graph.add_edge(tuple(np.round(line.coords[0], 9)),
                           tuple(np.round(line.coords[-1], 9)))
        return cells, unused, nx.number_connected_components(graph)

    def hole_count(cells):
        return sum(len(cell.interiors) for cell in cells
                   if not wide.covers(cell.representative_point()))

    cells, unused, components = measure(network)
    targets = np.unique(shapely.get_coordinates(network), axis=0)
    candidates = sorted(
        (np.linalg.norm(target - anchor), i, j)
        for i, anchor in enumerate(targets)
        for j, target in enumerate(targets[i + 1:], start=i + 1)
        if np.linalg.norm(target - anchor) > _GRID * 10
    )
    blocked = set()
    while unused > 1e-6 or hole_count(cells):
        holes = hole_count(cells)
        for index, (_, i, j) in enumerate(candidates):
            if index in blocked:
                continue
            anchor, target = targets[i], targets[j]
            connector = shapely.LineString([anchor, target])
            if not visible_region.covers(connector):
                blocked.add(index)
                continue
            endpoints = shapely.MultiPoint([anchor, target]).buffer(_GRID * 10)
            if not network.intersection(connector).difference(endpoints).is_empty:
                blocked.add(index)
                continue
            candidate = shapely.union_all([network, connector], grid_size=_GRID)
            next_cells, remaining, next_components = measure(candidate)
            if (remaining < unused - _LINE_TOLERANCE
                    or next_components < components or hole_count(next_cells) < holes):
                network = candidate
                cells, unused, components = next_cells, remaining, next_components
                break
        else:
            raise GeometryError(source, "Cannot open zero-width holes with visible connectors.")
    _validate_partition(cells, source)
    return cells


def offset_partitions(source):
    """Return convex perimeter/core faces and protected original-length centerlines.

    Depth and width come from internal ``geom`` configuration, not transform options.
    Coordinates are kept local during construction to limit numerical cancellation.
    """
    depth, width = geom.CORE_OFFSET_DEPTH, geom.CORE_MIN_WIDTH
    if not np.isfinite(depth) or depth <= 0 or not np.isfinite(width) or width <= 2e-4:
        raise ValueError("Core offset depth must be positive and minimum width must exceed 0.0002m.")
    source = shapely.force_2d(source)
    if source.is_empty or not source.is_valid or source.area <= 0:
        raise GeometryError(source, "Offset zoning requires a valid positive-area footprint.")
    origin = np.asarray(source.bounds[:2])
    local = shapely.set_precision(shapely.transform(source, lambda xy: xy - origin), _GRID)
    core = shapely.set_precision(local.buffer(-depth, join_style="mitre"), _GRID)
    wide, cuts = _collapse_narrow_core(local, core, width)
    perimeter, interior = [], []
    for cell in _partition_cells(local, wide, cuts):
        parts = [
            shapely.Polygon(face[:, :2])
            for face in _convex_parts_with_holes(
                np.asarray(cell.exterior.coords)[:-1],
                [np.asarray(ring.coords)[:-1] for ring in cell.interiors],
            )
        ]
        (interior if wide.covers(cell.representative_point()) else perimeter).extend(parts)
    _validate_partition(perimeter + interior, local, convex=True)
    validate_partition_lines(perimeter + interior, cuts)
    return tuple([
        shapely.transform(part, lambda xy: xy + origin) for part in group
    ] for group in (perimeter, interior, cuts))
