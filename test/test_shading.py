import tempfile

import numpy as np

from MoosasPy.model import MoosasModel
from MoosasPy.simulation.radiation.calculation import (
    _calculate_position_radiation_factors,
    write_radiation_geometry,
)
from MoosasPy.transform.geometry.element import MoosasGeometry
from MoosasPy.transform.geometry.geos import Ray, Vector
from MoosasPy.transform.stages.classification import (
    classify_horizontal_surfaces, classify_predefined_walls, classify_vertical_surfaces,
)
from MoosasPy.transform.stages.cleansing import attach_explicit_shading, cleanse_invalid_elements
from MoosasPy.transform.stages.glazing import attach_glazing_to_faces
from MoosasPy.utils import shapely


def test_attached_shading_blocks_radiation():
    def build_model(attach_shading):
        model = MoosasModel()
        model.geometryList = [
            MoosasGeometry(
                shapely.polygons(np.array([
                    [-2.0, -2.0, -1.0],
                    [2.0, -2.0, -1.0],
                    [2.0, 2.0, -1.0],
                    [-2.0, 2.0, -1.0],
                    [-2.0, -2.0, -1.0],
                ])),
                "floor",
                shapely.points([0.0, 0.0, 1.0]),
                4,
            ),
            MoosasGeometry(
                shapely.polygons(np.array([
                    [-1.0, -1.0, 1.0],
                    [1.0, -1.0, 1.0],
                    [1.0, 1.0, 1.0],
                    [-1.0, 1.0, 1.0],
                    [-1.0, -1.0, 1.0],
                ])),
                "shade",
                shapely.points([0.0, 0.0, -1.0]),
                -1,
            ),
        ]
        model.geoId = [geometry.faceId for geometry in model.geometryList]
        model, ids, walls, _ = classify_horizontal_surfaces(model)
        model = classify_predefined_walls(model, walls)
        model = classify_vertical_surfaces(model, ids)
        model = cleanse_invalid_elements(model)
        model = attach_glazing_to_faces(model)
        model = cleanse_invalid_elements(model)
        if attach_shading:
            model = attach_explicit_shading(model)
        return model

    factors = {}
    with tempfile.TemporaryDirectory() as work_dir:
        for attach_shading in (False, True):
            model = build_model(attach_shading)
            geo_path = write_radiation_geometry(model, work_dir=work_dir)
            factors[attach_shading] = _calculate_position_radiation_factors(
                Ray(Vector([0.0, 0.0, 0.0]), Vector([0.0, 0.0, 1.0])),
                [Vector([0.0, 0.0, 1.0])],
                geo_path=geo_path,
                reflection=0,
            )[0, 0]

    assert factors[False] == 1.0
    assert factors[True] == 0.0
