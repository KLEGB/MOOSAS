"""Main func of Moosas Geometry Transformation
Licence: Key Laboratory of Eco Planning & Green Building, Ministry of Education, Tsinghua University.
More information of this function:
https://doi.org/10.1007/s12273-023-1081-6
"""
from __future__ import annotations

import os
import os.path
import sys
import time

import shapely
import numpy as np

from .stages.assembly import assemble_model, pack_boundaries, partition_convex, partition_core
from .stages.boundary import prepare_boundary_geometry
from .stages.classification import (
    attach_unmatched_shading, classify_horizontal_surfaces, classify_predefined_walls,
    classify_vertical_surfaces, triangulate_horizontal_faces,
)
from .stages.cleansing import (
    attach_explicit_shading, cleanse_coplanar_lines, cleanse_duplicate_walls,
    cleanse_invalid_elements, cleanse_overlapping_elements,
)
from .stages.convexification import convexify_model
from .stages.finalization import attach_shading_content, normalize_air_boundaries
from .stages.generation import CCRSpaceGeneration
from .stages.glazing import attach_glazing_to_faces
from .options import TransformOptions
from .stages.splitting import split_vertical_walls, split_wall_intersections
from .stages.standardization import standardize_model
from .stages.topology import build_face_topology, build_space_topology, connect_void_spaces
from .stages.validation import validate_model
from .importers.geo import _readGeo, preClassified
from .importers.obj import _readObj
from .importers.stl import _readStl
from ..model import MoosasModel
from ..model.building_templates import configure_model_resources


def _sort_spaces(model: MoosasModel) -> MoosasModel:
    """Order generated spaces deterministically by level and plan position."""
    def sort_key(space):
        centroid = shapely.centroid(space.force_2d())
        return (
            float(space.level),
            float(centroid.y),
            float(centroid.x),
            str(space.id),
        )

    model.spaceList = sorted(model.spaceList, key=sort_key)
    return model

def _load_geometry_source(input_path: str, input_type: str | None = None) -> MoosasModel:
    """Create the draft geometry model consumed exclusively by ``transform``."""
    suffix = (input_type or os.path.splitext(input_path)[1].lstrip('.')).lower()
    model = MoosasModel()
    if suffix == 'geo':
        model.geometryList = _readGeo(input_path)
    elif suffix == 'obj':
        model.geometryList = _readObj(input_path)
    elif suffix == 'stl':
        model.geometryList = _readStl(input_path)
    else:
        raise ValueError(f"Unsupported geometry source format: {suffix or input_path}")
    return preClassified(model)


def transform(
    input_path: str,
    input_type: str | None = None,
    *,
    method=CCRSpaceGeneration,
    options: TransformOptions | None = None,
    stdout=sys.stdout,
) -> MoosasModel:
    """
    Convert geometric data to structured spatial model with optional processing.

    Parameters
    ----------
    input_path : str
        Path to input geometry file. Supported formats:
        - *.obj : Wavefront OBJ format
        - *.stl : STL format (future support)
        - *.geo : Stream format (future support)

    input_type : str, optional
        Explicit geometry format specification: ``geo``, ``obj``, or ``stl``.
        Auto-detected from input_path suffix if None.

    method : callable, optional
        Space generation algorithm (default: CCRSpaceGeneration). Options:
        - VFGSpaceGeneration (L. Jones 2013)
        - BTGSpaceGeneration (H. Chen 2018)
        - CCRSpaceGeneration (J. Xiao 2023)

    options : TransformOptions, optional
        Shared configuration for classification, cleansing, assembly, and finalization.

    stdout : object, optional
        Output stream for transformation logs (default: sys.stdout).

    Returns
    -------
    MoosasModel
        Structured spatial model with properties (More information could be found in models module):
        - spacesList : List[MoosasSpace] - Spatial units with thermal properties
        - wallList : List[MoosasWall] - Architectural components
        - buildingTemplate : dict -  dictionary of the termal building templates and properties
        - weather : WeatherData - hourly weather series and location metadata

    Examples
    --------
    >>> from MoosasPy.transform.stages.generation import CCRSpaceGeneration
    >>> model = transform('test.obj', method=CCRSpaceGeneration)
    >>> model.save('output.xml')

    Energy analysis example:
    >>> from MoosasPy.simulation.energy import EnergyRunner
    >>> result = EnergyRunner(model=model, weather=weather).run()
    >>> data = result.data
    >>> print(f"Total energy demand: {data['total']['cooling'] + data['total']['heating']} kWh")

    Notes
    -----
    GEO, OBJ, and STL are the only accepted source formats. Model-file loading
    and saving are exposed by :class:`MoosasModel`.
    """

    # redirect stdout
    if isinstance(stdout, str):
        if os.path.isfile(stdout):
            stdout = open(stdout, 'w+')
    sysout = sys.stdout
    sys.stdout = stdout
    t0 = time.time()
    # load model from file
    print('LOADING: ', end='')
    model = _load_geometry_source(input_path, input_type)
    configure_model_resources(model)
    print('import face number:', len(model.geoId))

    if model is None:  # zero len space will cause serve errors
        return

    options = options or TransformOptions()

    if options.simplify_boundary:
        model = prepare_boundary_geometry(model)

    did_convexify = False
    while True:
        t1 = time.time()

        if options.triangulate_faces:
            model = triangulate_horizontal_faces(model)
        
        model, classified_ids, predefined_walls, unmatched = classify_horizontal_surfaces(model)
        
        if options.attach_shading:
            model = attach_unmatched_shading(model, unmatched)
        
        model = classify_predefined_walls(model, predefined_walls)
        
        if options.break_wall_vertical:
            model = split_vertical_walls(model, classified_ids)
        
        model = classify_vertical_surfaces(model, classified_ids)
        model.faceList = np.array(model.faceList)
        model.wallList = np.array(model.wallList)
        model.glazingList = np.array(model.glazingList)

        if options.solve_redundant:
            model = cleanse_coplanar_lines(model)
        model = cleanse_invalid_elements(model)
        model = attach_glazing_to_faces(model)
        if options.solve_redundant:
            model = cleanse_coplanar_lines(model)
        if options.solve_duplicated:
            model = cleanse_duplicate_walls(model)
        model = cleanse_invalid_elements(model)
        if options.solve_overlap:
            model = cleanse_overlapping_elements(model)
        if options.break_wall_horizontal:
            model = split_wall_intersections(model)
            if options.solve_duplicated:
                model = cleanse_duplicate_walls(model)
        if options.attach_shading:
            model = attach_explicit_shading(model)
        t2 = time.time()
        t3 = time.time()

        if options.divided_zones and not did_convexify:
            model = convexify_model(model)
            did_convexify = True
            continue

        break

    model = method(model)
    t4 = time.time()

    model = pack_boundaries(model)
    if options.insert_core:
        model = partition_core(model)
    elif options.divided_zones:
        model = partition_convex(model)
    print()
    print('PACKING: Identified boundaries', len(model.edgeList))
    model = assemble_model(model, solve_overlap=options.solve_overlap)

    t5 = time.time()

    if options.break_wall_vertical:
        model = connect_void_spaces(model)
    model = build_space_topology(model)
    model = build_face_topology(model)
    if options.attach_shading:
        model = attach_shading_content(model)
    if options.standardize:
        model = standardize_model(model)
    model = normalize_air_boundaries(model)
    model = validate_model(model)
    model = _sort_spaces(model)
    t6 = time.time()
    t7 = time.time()

    print("-" * 20)
    print('Program finish. Summary:')
    model.setCategory(False)
    model.summary()
    print('-' * 20)
    print(f"I/O                {'%.3fs' % (t1 - t0)}\t{'%.1f' % ((t1 - t0) / (t7 - t0) * 100)}%\t",
          '\u25A0' * int((t1 - t0) / (t7 - t0) * 50))
    print(f"Data Structuring   {'%.3fs' % (t2 - t1)}\t{'%.1f' % ((t2 - t1) / (t7 - t0) * 100)}%\t",
          '\u25A0' * int((t2 - t1) / (t7 - t0) * 50))
    print(f"Data Cleansing     {'%.3fs' % (t3 - t2)}\t{'%.1f' % ((t3 - t2) / (t7 - t0) * 100)}%\t",
          '\u25A0' * int((t3 - t2) / (t7 - t0) * 50))
    print(f"1LSB Calculation   {'%.3fs' % (t4 - t3)}\t{'%.1f' % ((t4 - t3) / (t7 - t0) * 100)}%\t",
          '\u25A0' * int((t4 - t3) / (t7 - t0) * 50))
    print(f"Space Construction {'%.3fs' % (t5 - t4)}\t{'%.1f' % ((t5 - t4) / (t7 - t0) * 100)}%\t",
          '\u25A0' * int((t5 - t4) / (t7 - t0) * 50))
    print(f"2LSB Calculation   {'%.3fs' % (t6 - t5)}\t{'%.1f' % ((t6 - t5) / (t7 - t0) * 100)}%\t",
          '\u25A0' * int((t6 - t5) / (t7 - t0) * 50))
    print(f"Content attachment {'%.3fs' % (t7 - t6)}\t{'%.1f' % ((t7 - t6) / (t7 - t0) * 100)}%\t",
          '\u25A0' * int((t7 - t6) / (t7 - t0) * 50))
    print(f"Total Duration     {'%.3fs' % (t7 - t0)}\t100%")

    sys.stdout = sysout
    return model
