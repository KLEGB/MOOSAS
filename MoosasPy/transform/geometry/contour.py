"""Ver0.4.3.3 new contour calculation module, more serious and stable"""
from __future__ import annotations

from .geos import *
from ...model.boundary import MoosasEdge
from .element import MoosasWall, MoosasGlazing
from .convexify import convexify_shape
from .core import offset_partitions, validate_partition_lines
from ...utils import searchBy, shapely, np, TopologyError, mixItemListToList
from ...utils.constant import geom
from .planar_graph import TopoNode, TopoBound, TopoEdge, TopoNetwork
from ...model.container import MoosasContainer


def _unique_boundaries(boundaries):
    """Record each closed region once, regardless of loop start/direction."""
    unique = []
    for boundary in boundaries:
        geometry = boundary.geometry
        if shapely.get_dimensions(geometry) != 2 or shapely.area(geometry) <= geom.POINT_PRECISION ** 2:
            continue
        if not any(shapely.equals(geometry, kept.geometry) for kept in unique):
            unique.append(boundary)
    return unique


def outerBoundary(model: MoosasContainer, bld_level: float) -> list[shapely.Geometry]:
    """
    Calculate the outer boundary of a network at a specified building level.
    
    Parameters
    ----------
    model : MoosasContainer
        The model containing topological edges to retrieve the network from.
    bld_level : float
        The building level at which to retrieve the network.
    
    Returns
    -------
    list[shapely.Geometry]
        A list of shapely Geometry objects representing the outer boundaries of each network component.
    """
    """only Calculate the outer boundary of a network

     ---------------------------------
    bld_level: building level to retrieve in float
    model: get topoEdge from this model

    return: list[shapely.Geometry]
    """

    network = TopoNetwork.inLevel(bld_level, model)
    if network.edges is None or network.nodes is None:
        return None
    networks = TopoNetwork.splitNetwork(network)
    print(f'\rTOPOLOGY: in {bld_level}: Calculate outer Boundary', end='')
    """calculate the outer boundaries (the biggest boundaries) of each network"""
    boundaries = [network.outerBoundary() for network in networks]
    return [bound.geometry for boundList in boundaries for bound in boundList]


def _partition_network(network):
    """Extract bounded faces from the existing planar graph without losing area.

    Polygonization ignores dangling/bridge edges as cuts. Nested rings remain
    separate boundaries and are assigned as holes during space assembly.
    No virtual walls or geometry beyond the input enclosure are introduced.
    """
    lines = [shapely.linestrings(shapely.get_coordinates([edge.fromLocation, edge.toLocation])) for edge in network.edges]
    polygons = shapely.get_parts(shapely.polygonize(lines))
    by_location = {tuple(shapely.get_coordinates(node.location)[0]): node for node in network.nodes}
    boundaries = []
    for polygon in polygons:
        # Interior rings have their own bounded polygon in polygonize's output.
        coordinates = shapely.get_coordinates(shapely.get_exterior_ring(polygon))
        nodes = [by_location[tuple(point)] for point in coordinates]
        boundaries.append(TopoBound(nodes))
    return _unique_boundaries(boundaries)


def closed_contour_calculation(model: MoosasContainer, bld_level: float) -> MoosasContainer:
    """
    Calculate closed contours at a specified building level and update the model with boundary information.
    
    Parameters
    ----------
    model : MoosasContainer
        The input model containing topological edges to be processed.
    bld_level : float
        The building level at which to compute the closed contours.
    
    Returns
    -------
    MoosasContainer
        The updated model with recorded boundary information from the contour calculation.
    """
    network = TopoNetwork.inLevel(bld_level, model)
    if network.edges is None or network.nodes is None:
        return model

    networks = TopoNetwork.splitNetwork(network)
    print(f'\rTOPOLOGY: in {bld_level}: Dividing boundary', end='')
    boundaries = [bound for network in networks for bound in _partition_network(network)]
    print(f'\rTOPOLOGY: in {bld_level}: find {len(boundaries)} boundaries')
    return _documentBoundary(boundaries, model)


def _documentBoundary(boundaries: Iterable[TopoBound], model: MoosasContainer) -> MoosasContainer:
    """
    Reverse boundary orientation if necessary and append boundary edges to model.
    
    Parameters
    ----------
    boundaries : Iterable[TopoBound]
        An iterable of TopoBound objects representing boundaries, each containing a geometry and edge loop.
    model : MoosasContainer
        The container model to which boundary edge lists will be added.
    
    Returns
    -------
    MoosasContainer
        The updated model with boundary edge lists appended to its boundaryList attribute.
    """
    for i, bound in enumerate(boundaries):
        if not is_ccw(bound.geometry):
            bound.reverse()
        model.boundaryList.append([model.wallList[edge.modelId] for edge in bound.edgeLoop])
    return model


def _partition_needs_merge(partition):
    """Identify small zones or thin strips independently of their orientation."""
    if partition.area < geom.ROOM_MIN_AREA:
        return True
    rectangle = shapely.get_coordinates(shapely.minimum_rotated_rectangle(partition))
    lengths = np.linalg.norm(np.diff(rectangle, axis=0), axis=1)
    width = np.min(lengths)
    return (
        width < geom.PARTITION_SLIVER_MAX_WIDTH
        and np.max(lengths) > geom.PARTITION_SLIVER_MIN_ASPECT_RATIO * width
    )


def _merge_tiny_partitions(partitions, protected_lines=()):
    """Absorb small/slender zones without deleting protected zero-width holes."""
    partitions = list(partitions)
    protected = shapely.union_all(protected_lines)
    while True:
        selected = None
        tiny_indices = sorted(
            (index for index, part in enumerate(partitions) if _partition_needs_merge(part)),
            key=lambda index: (partitions[index].area, index),
        )
        for tiny_index in tiny_indices:
            tiny = partitions[tiny_index]
            best = None
            for neighbor_index, neighbor in enumerate(partitions):
                if neighbor_index == tiny_index:
                    continue
                shared = tiny.boundary.intersection(neighbor.boundary).length
                if shared <= geom.POINT_PRECISION:
                    continue
                merged = tiny.union(neighbor)
                if merged.geom_type != "Polygon" or merged.interiors or not merged.is_valid:
                    continue
                swallowed = protected.intersection(merged).difference(merged.boundary.buffer(1e-7))
                if swallowed.length > 1e-7:
                    continue
                score = (-shared, -neighbor.area, neighbor_index)
                if best is None or score < best[0]:
                    best = (score, neighbor_index, merged)
            if best is not None:
                _, neighbor_index, merged = best
                selected = (tiny_index, neighbor_index, merged)
                break
        if selected is None:
            for index in tiny_indices:
                print(
                    "******Warning: small or slender zone "
                    f"({partitions[index].area:g} m2) has no eligible edge neighbor; "
                    "preserving its geometry (protected edges cannot be removed)"
                )
            return partitions
        tiny_index, neighbor_index, merged = selected
        partitions = [
            part for index, part in enumerate(partitions)
            if index not in (tiny_index, neighbor_index)
        ] + [merged]


def _direct_child_holes(edge, edges):
    """Return only the immediately nested boundaries inside an edge."""
    boundary = edge.force_2d()
    contained = [
        candidate.force_2d()
        for candidate in edges
        if candidate is not edge
        and shapely.contains_properly(boundary, candidate.force_2d())
    ]
    return [
        candidate
        for candidate in contained
        if not any(
            other is not candidate
            and shapely.contains_properly(other, candidate)
            for other in contained
        )
    ]


def overhead_projection(model: MoosasContainer, level: float) -> shapely.Geometry:
    """Project physical floors, roofs and skylights above a storey."""
    return shapely.union_all([
        face.force_2d()
        for face in list(model.faceList) + list(model.skylightList)
        if face.category in (0, 1)
        and face.elevation > level + geom.POINT_PRECISION
        and abs(float(face.normal[2])) >= geom.HORIZONTAL_ANGLE_THRESHOLD
    ])


def _replace_one_sided_air_boundaries(boundaries, walls):
    """Use a coincident opaque wall when an air wall bounds only one contour."""
    occurrence_count = {}
    for boundary in boundaries:
        for wall in boundary:
            occurrence_count[wall] = occurrence_count.get(wall, 0) + 1

    replacements = {}
    opaque_walls = [wall for wall in walls if not wall.is_air_boundary]
    for wall, count in occurrence_count.items():
        if not wall.is_air_boundary or count != 1:
            continue
        replacement = next(
            (
                candidate
                for candidate in opaque_walls
                if candidate.level == wall.level
                and equals(candidate.force_2d(), wall.force_2d())
            ),
            None,
        )
        if replacement is not None:
            replacements[wall] = replacement

    return [
        [replacements.get(wall, wall) for wall in boundary]
        for boundary in boundaries
    ]


def _is_internal_partition(segment, source_boundary, walls):
    """Return whether a generated partition is not backed by a physical wall."""
    boundary_parts = [source_boundary]
    opaque_walls = [wall for wall in walls if not wall.is_air_boundary]
    boundary_parts.extend(wall.force_2d() for wall in opaque_walls)
    physical_boundary = shapely.union_all(boundary_parts)
    return not shapely.covered_by(
        segment,
        shapely.buffer(physical_boundary, geom.POINT_PRECISION * 10),
    )


def packing_edges(model: MoosasContainer, divided_zones, *, insert_core=False) -> MoosasContainer:
    """
    Packs edges into a MoosasContainer by validating and processing boundary lists, and optionally subdividing complex faces into simpler polygons.
    
    Parameters
    ----------
    model : MoosasContainer
        The container object holding wall, edge, boundary, and level lists to be processed.
        Modified in place by appending valid edges and walls, and removing processed ones.
    divided_zones : bool
        If True, enables convex subdivision of complex footprints.
        Air walls are added to represent internal divisions, and original edges are replaced with new constructed edges.
    insert_core : bool
        Enable offset thermal zoning with opaque core/perimeter interfaces.
        Other subdivisions and collapsed narrow-core centerlines remain air boundaries.
    
    Returns
    -------
    MoosasContainer
        The updated model with validated and potentially subdivided edges, newly added air walls (if applicable),
        and remaining unassigned walls marked in `wall_remain`.
    """

    model.boundaryList = _replace_one_sided_air_boundaries(
        model.boundaryList,
        model.wallList,
    )
    faceSet = set(model.wallList)
    for edge in model.boundaryList:
        # print(edge)
        if len(edge) < 3:
            print("******Warning: TopologyError, boundary less than 3 edges")
            continue
        try:
            the_edge = MoosasEdge(edge)
            if the_edge.is_valid():
                model.edgeList.append(the_edge)
                faceSet.difference_update(edge)
            # else:
            #     print([e.force_2d() for e in edge])

        except GeometryError:
            print("******Warning: GeometryError, something occurred and the boundary was skipped")


    """Divide the boundaries into simple polygons"""
    if divided_zones or insert_core:
        model.edgeList = [
            edge for edge in model.edgeList
            if edge.area >= geom.ROOM_MIN_AREA
        ]
        for levelIdx, bldLevel in enumerate(model.levelList):
            edges = np.array(model.edgeList)[searchBy('level', bldLevel, model.edgeList)]
            overhead = overhead_projection(model, bldLevel)
            for edgeIdx, edge in enumerate(edges):
                # ``contains`` considers a geometry to contain itself.  That
                # made the current boundary an identical "hole", so a
                # concave footprint was passed to the convexifier with its
                # exterior removed and could not be divided.  A hole must lie
                # strictly inside the boundary instead.
                holes = _direct_child_holes(edge, edges)
                footprint = shapely.difference(edge.force_2d(), shapely.union_all(holes))
                if shapely.area(shapely.intersection(
                    footprint, overhead, grid_size=geom.POINT_PRECISION
                )) <= geom.AREA_PRECISION:
                    continue
                if insert_core:
                    perimeter, core, cuts = offset_partitions(footprint)
                    core_boundary = shapely.buffer(
                        shapely.boundary(shapely.union_all(core)),
                        geom.POINT_PRECISION * 10,
                    )
                    newEdges = (
                        _merge_tiny_partitions(perimeter, cuts)
                        + _merge_tiny_partitions(core, cuts)
                    )
                    validate_partition_lines(newEdges, cuts)
                else:
                    newEdges, _ = convexify_shape(edge.force_2d(), holes)
                    newEdges = _merge_tiny_partitions(newEdges)
                if len(newEdges) > 1:
                    walls = list(np.array(model.wallList)[searchBy('level', bldLevel, model.wallList)])
                    source_boundary = shapely.boundary(edge.force_2d())
                    for new_edge in newEdges:
                        coordinates = shapely.get_coordinates(new_edge)
                        for start, end in zip(coordinates[:-1], coordinates[1:]):
                            segment = shapely.linestrings([start, end])
                            if shapely.length(segment) <= geom.POINT_PRECISION:
                                continue
                            if MoosasEdge.matchWall(segment, walls) is None:
                                is_internal_partition = _is_internal_partition(
                                    segment,
                                    source_boundary,
                                    walls,
                                )
                                is_core_interface = insert_core and shapely.covered_by(
                                    segment, core_boundary,
                                )
                                partition_wall = MoosasWall.fromProjection(
                                    segment,
                                    bottom=bldLevel,
                                    top=model.levelList[levelIdx + 1],
                                    model=model,
                                    airBoundary=is_internal_partition and not is_core_interface,
                                )
                                model.wallList = np.append(model.wallList, partition_wall)
                                walls.append(partition_wall)
                    newConstructEdges = []
                    for ed in newEdges:
                        try:
                            newConstructEdges += [MoosasEdge.selectWall(ed, walls)]
                        except GeometryError as gE:
                            print(f"******Warning: {gE}")
                    newEdges = newConstructEdges
                    model.edgeList.remove(edge)
                    model.edgeList += newEdges
                print(f'\rTOPOLOGY: in {bldLevel}: Dividing zones {edgeIdx}/{len(edges)}', end='')

    model.wall_remain = list(faceSet)
    print()
    print('PACKING: Identified boundaries', len(model.edgeList))
    return model
