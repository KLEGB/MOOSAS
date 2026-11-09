import numpy as np
import os
import shapely

from ...utils import Iterable
from ...utils.tools import path
from ...transform.geometry.element import MoosasGlazing

from ...transform.geometry.geos import Vector


def _modelBoundBox(model) -> np.ndarray:
    """calculate the model's boundary box by MoosasSpace.boundBox()"""
    bbox = np.array([s.boundBox() for s in model.spaceList])
    bboxMin = np.min(bbox[:, 0], axis=0)
    bboxMax = np.max(bbox[:, 1], axis=0)
    return np.array([bboxMin, bboxMax])


def _calculate_orientation(normal) -> int:
    """
    Calculate the orientation of a facade based on its normal vector.
    
    Parameters
    ----------
    normal : Vector
        A vector object representing the normal direction of the facade. 
        Must have an attribute `array` that returns a 3-element array-like 
        containing the x, y, z components of the vector.
    
    Returns
    -------
    int
        The orientation angle in degrees, ranging from 0 to 360, representing 
        the compass direction of the facade. Returns 0 if the normal is parallel 
        to the positive z-axis ([0,0,1]).
    """
    if Vector.parallel(normal,[0,0,1]):
        return 0
    normal = normal.array
    """calculate facade orientation by its factor"""
    orientation = np.arccos((-1) * (normal[0]) / np.sqrt((normal[0]) ** 2 + (normal[1]) ** 2)) * 180 / np.pi
    if normal[1] > 0:
        orientation = 360 - orientation
    if orientation == 360:
        orientation = 0
    return orientation


def pressureInput(windVector: Vector, glazing: MoosasGlazing, *, model_bounds=None):
    """
        Xgb pressure prediction input
        wind_direction: wind dir in DEGREE
        buildingBoundBox: given by modelBoundBOx()
        wall: the facade wall that glazing belongs to.
        glazing: the glazing/aperture.

        Return Value [WidthToDepth, heightToDepth, theta, height, horizon]
        WidthToDepth: BuildingWidth/BuildingDepth considering the glazing orientation
        heightToDepth: BuildingHeight/BuildingDepth considering the glazing orientation
        theta: (orientation - windDirection)/180
        height: aperture vertical location of the facade, standardize (0..1)
        horizon: aperture horizontal location of the facade, standardize (0..1)
    """
    buildBoundBox = _modelBoundBox(glazing.parent) if model_bounds is None else model_bounds
    buildingSize3d = [
        buildBoundBox[1][0] - buildBoundBox[0][0],
        buildBoundBox[1][1] - buildBoundBox[0][1],
        buildBoundBox[1][2] - buildBoundBox[0][2]
    ]
    ori = _calculate_orientation(glazing.orientation)
    WidthToDepth, heightToDepth, verticalDimensionIdx, reverse = 0, 0, 0, 0
    if (45 < ori <= 135) or (225 < ori <= 315):
        WidthToDepth, heightToDepth = buildingSize3d[1] / buildingSize3d[0], buildingSize3d[2] / buildingSize3d[0]
    else:
        WidthToDepth, heightToDepth = buildingSize3d[0] / buildingSize3d[1], buildingSize3d[2] / buildingSize3d[1]
        verticalDimensionIdx = 1

    theta = abs(ori - windVector.azimuth(False))
    if theta > 180:
        theta = 360 - theta
    WidthToDepth = round((WidthToDepth - 0.4) / 2.1, 2)
    heightToDepth = round((heightToDepth - 0.1) / 0.9, 2)
    theta = round(theta / 180, 2)
    WidthToDepth = min(max(0, WidthToDepth), 1)
    heightToDepth = min(max(0, heightToDepth), 1)

    if ori <= 45 or ori > 225:
        reverse = 1

    """boundary box of the wall faces"""
    wallBoundBox = [1e+9, -1e+9, 1e+9, -1e+9]
    for v in shapely.get_coordinates(glazing.parentFace.face, include_z=True):
        ht, hn = round(float(v[2]), 2), round(float(v[verticalDimensionIdx]), 2)
        wallBoundBox[0] = min(ht, wallBoundBox[0])
        wallBoundBox[1] = max(ht, wallBoundBox[1])
        wallBoundBox[2] = min(hn, wallBoundBox[2])
        wallBoundBox[3] = max(hn, wallBoundBox[3])

    """weight center of the glazing faces"""
    vMean = np.mean(shapely.get_coordinates(glazing.face, include_z=True)[:-1], axis=0)
    height = round((vMean[2] - wallBoundBox[0]) / (wallBoundBox[1] - wallBoundBox[0]), 2)
    horizon = round((vMean[verticalDimensionIdx] - wallBoundBox[2]) / (wallBoundBox[3] - wallBoundBox[2]), 2)

    if reverse == 1:
        horizon = 1 - horizon

    return [WidthToDepth, heightToDepth, theta, height, horizon]


def callXgb(xgbInput, xgbOutput=None) -> np.ndarray:
    """
        Call xgboost for pressure prediction.

        ----------------------------------
        It has 2 workflow that:
        xgbInput: str input file path
        xgbOutput: str output file path

        or:
        XgbInput: np.ndarry input matrix
        XgbOutput: None
        and return the output matrix as np.ndarry

        -----------------------------------
        It has 5 inputs as:
        [WidthToDepth, heightToDepth, theta, height, horizon]
        WidthToDepth: BuildingWidth/BuildingDepth considering the glazing orientation
        heightToDepth: BuildingHeight/BuildingDepth considering the glazing orientation
        theta: (orientation - windDirection)/180
        height: aperture vertical location of the facade, standardize (0..1)
        horizon: aperture horizontal location of the facade, standardize (0..1)

        ------------------------------------
        The output value is the Wind Pressure Coefficient (Wp) which can be used to calculate pressure:

        P = Wp * airDensity * velocity^2 * ((altitude/10)^(alpha * 2)) / 2
        alpha = 0.22
        airDensity = 1.205
    """
    from xgboost import Booster, DMatrix
    bst = Booster()
    bst.load_model(os.path.join(path.dataBaseDir, 'airflow', 'xgb.json'))
    inputs = []
    if isinstance(xgbInput, str):
        with open(xgbInput, 'r') as f:
            file = f.read().split('\n')
        # get input
        inputs = []
        for i in file:
            if len(i) != 0:
                if i[0] != '!':
                    inputs.append(np.array(i.split(',')).astype(float))
    else:
        inputs = xgbInput

    # predict
    inputs = np.asarray(inputs, dtype=float)
    if not np.all(np.isfinite(inputs)):
        raise ValueError('Invalid facade geometry produced non-finite wind pressure inputs')
    outputs = bst.predict(DMatrix(inputs, feature_names=bst.feature_names))
    # get output
    if xgbOutput is not None:
        text = ""
        for o in outputs:
            text += str(round(o * 2.4588996 - 1.4493129, 2)) + "\n"
        with open(xgbOutput, 'w') as f:
            f.writelines(text[:-1])
    return outputs
