import re
import os

from eppy.modeleditor import IDF

from .construction import Construction
from .model import *
from ....transform.geometry.convexify import convexify_shape
from ....transform.geometry.element import MoosasSpace, MoosasElement
from ....transform.geometry.geos import ccwNormal, Vector, offset, trim, projectTo
from ....utils import shapely, path
from ....utils.constant import geom
from .parser import ZoneTemplate

def createThermalSurface(idf: IDF, element: MoosasElement, surfaceType='Floor',
                         Construction_Name="Office_External_Wall",
                         Construction_Name_Window="Office_External_Window",
                         normal=None, encodeWindow=True):
    """
    Create a thermal surface in an EnergyPlus IDF file based on a MoosasElement.
    
    Parameters
    ----------
    idf : IDF
        The EnergyPlus Input Data File (IDF) object to which the thermal surface will be added.
    element : MoosasElement
        The building element (e.g., wall, floor) used to create the thermal surface. Must have valid space and geometric properties.
    surfaceType : str, optional
        Type of the surface, one of 'Floor', 'Wall', 'Ceiling', or 'Roof'. Default is 'Floor'.
    Construction_Name : str, optional
        Name of the construction used for the main surface. Default is "Office_External_Wall".
    Construction_Name_Window : str, optional
        Name of the construction used for any associated window surfaces. Default is "Office_External_Window".
    normal : Vector, optional
        Outward normal for the first zone. If None, it is determined from the
        geometry and surface type. Vertices are counterclockwise from outside.
    
    Returns
    -------
    list
        A list of IDF objects (surfaces) created, including the main thermal surface and any associated window surfaces. Returns None if the element is invalid or belongs to a void space.
    """
    model = element.parent
    space0 = model.spaceIdDict[element.space[0]]
    if len(element.space) == 2:
        if space0.is_void():
            element.isOuter = True
            space0 = model.spaceIdDict[element.space[1]]
        if model.spaceIdDict[element.space[1]].is_void():
            element.isOuter = True
    elif len(element.space) == 1:
        if space0.is_void():
            return None
    else:
        return None
    if surfaceType == 'Floor':
        if element in space0.ceiling.face:
            surfaceType = 'Ceiling'

    ThermalSettings = MoosasSettings(default=FaceDefault)
    kwargs = {'Name': element.space[0] + '-' + element.Uid,
              "Zone_Name": element.space[0],
              "Surface_Type": surfaceType,
              "Construction_Name": Construction_Name}
    if element.isOuter:
        if surfaceType == 'Floor' and (element.parent.levelList.index(element.level) == 0):
            kwargs["Outside_Boundary_Condition"] = 'Ground'
            kwargs["Sun_Exposure"] = 'NoSun'
            kwargs["Wind_Exposure"] = 'NoWind'
            kwargs["View_Factor_to_Ground"] = '0'
            encodeWindow = False
        else:
            kwargs["Outside_Boundary_Condition"] = 'Outdoors'
            kwargs["Sun_Exposure"] = 'SunExposed'
            kwargs["Wind_Exposure"] = 'WindExposed'
            kwargs["View_Factor_to_Ground"] = 'AutoCalculate'
    else:
        kwargs["Outside_Boundary_Condition"] = 'Surface'
        kwargs["Outside_Boundary_Condition_Object"] = element.space[1] + '-' + element.Uid
        kwargs["Sun_Exposure"] = 'NoSun'
        kwargs["Wind_Exposure"] = 'NoWind'
        kwargs["View_Factor_to_Ground"] = '0'
    ThermalSettings.updateParams(**kwargs)
    if normal is None:
        if surfaceType in {'Floor', 'Ceiling', 'Roof'}:
            normal = ccwNormal(element.representation())
            if (normal.z > 0) == (surfaceType == 'Floor'):
                normal = -normal
        else:
            try:
                normal = space0.edge.FactorOfWall[space0.edge.wall.index(element)]
            except ValueError:
                normal = element.normal
    encodeFace(ThermalSettings, element.representation(), normal)
    # create objects
    surface1 = ThermalSettings.applyToIDF(idf)
    faceObject = [surface1]
    if not element.isOuter:
        ThermalSettings.params["Name"] = element.space[1] + '-' + element.Uid
        ThermalSettings.params["Zone_Name"] = element.space[1]
        ThermalSettings.params["Outside_Boundary_Condition_Object"] = element.space[0] + '-' + element.Uid
        encodeFace(ThermalSettings, element.representation(), -normal)
        if surfaceType == 'Floor':
            surfaceType = 'Ceiling'
        elif surfaceType == 'Ceiling':
            surfaceType = 'Floor'
        ThermalSettings.params["Surface_Type"] = surfaceType
        surface2 = ThermalSettings.applyToIDF(idf)
        faceObject.append(surface2)

    if encodeWindow:
        for gls in element.glazingElement:
            faceObject += createWindowSurface(idf, gls, element, Construction_Name_Window, normal=normal)
    return faceObject


def encodeFace(obj: MoosasSettings, polygon: shapely.Geometry, normal: Vector):
    """
    Encode face geometry into a given settings object by storing vertex coordinates.
    
    Parameters
    ----------
    obj : MoosasSettings
        The settings object where face parameters will be stored.
    polygon : shapely.Geometry
        A polygonal geometry whose coordinates define the face.
    normal : Vector
        A vector used to determine the orientation of the face; 
        if the dot product with the face normal is negative, vertex order is reversed.
    
    Returns
    -------
    None
        This function modifies the `obj` in place and does not return a value.
    """
    coordinates = shapely.get_coordinates(polygon, include_z=True)[:-1]
    for field in [field for field in obj.params if field.startswith('Vertex_')]:
        del obj.params[field]
    obj.params['Number_of_Vertices'] = len(coordinates)
    if Vector.dot(ccwNormal(polygon), normal) < 0:
        coordinates = coordinates[::-1]
    coordinates = np.round(coordinates / geom.POINT_PRECISION) * geom.POINT_PRECISION
    for i, point in enumerate(coordinates):
        obj.params[f'Vertex_{i + 1}_Xcoordinate'] = point[0]
        obj.params[f'Vertex_{i + 1}_Ycoordinate'] = point[1]
        obj.params[f'Vertex_{i + 1}_Zcoordinate'] = point[2]


def createWindowSurface(idf: IDF, element: MoosasElement, parentElement: MoosasElement,
                        Construction_Name="Office_External_Wall",
                        normal=None):
    """
    Create window surface(s) in an EnergyPlus IDF file based on element geometry and thermal settings.
    
    Parameters
    ----------
    idf : IDF
        The EnergyPlus Input Data File (IDF) object to which the surface will be added.
    element : MoosasElement
        The element representing the window geometry to be encoded.
    parentElement : MoosasElement
        The parent building element (e.g., wall) that hosts the window; used to derive space and boundary information.
    Construction_Name : str, optional
        The name of the construction to be assigned to the window surface. Default is "Office_External_Wall".
    normal : array-like, optional
        The normal vector to the surface face; used during geometry encoding. If not provided, inferred from geometry.
    
    Returns
    -------
    list of Surface
        A list containing one or two Surface objects added to the IDF:
        - One surface for outer (exterior) parent elements.
        - Two surfaces (with opposite orientations and linked boundary conditions) for inner (interior) parent elements.
    """
    faceObjects = []
    face = element.representation()

    # project the face to its parent
    face = projectTo(face,parentElement.representation())

    # clip by the parent surface
    face = trim(face, parentElement.representation())

    if face is None:
        return []

    # split the face into 4 coordinates
    triFaces, _ = convexify_shape(face)
    for idx, triFace in enumerate(triFaces):

        # offset the window considering the splitter
        triFace = offset(triFace, -0.1)
        if triFace is None or shapely.is_empty(triFace):
            continue

        # project the face to its parent
        triFace = projectTo(triFace, parentElement.representation())
        if triFace is None or shapely.is_empty(triFace):
            continue

        kwargs = {'Name': parentElement.space[0] + '-' + parentElement.Uid + '-' + element.Uid + '-' + str(idx),
                  "Building_Surface_Name": parentElement.space[0] + '-' + parentElement.Uid,
                  "Construction_Name": Construction_Name}
        ThermalSettings = MoosasSettings(default=WindowDefault, **kwargs)
        encodeFace(ThermalSettings, triFace, normal)

        if not parentElement.isOuter:
            ThermalSettings.params["Outside_Boundary_Condition_Object"] = parentElement.space[
                                                                              1] + '-' + parentElement.Uid + '-' + element.Uid + '-' + str(
                idx)
            surface1 = ThermalSettings.applyToIDF(idf)
            kwargs = {'Name': parentElement.space[1] + '-' + parentElement.Uid + '-' + element.Uid + '-' + str(idx),
                      "Building_Surface_Name": parentElement.space[1] + '-' + parentElement.Uid,
                      "Outside_Boundary_Condition_Object": parentElement.space[
                                                               0] + '-' + parentElement.Uid + '-' + element.Uid + '-' + str(
                          idx),
                      "View_Factor_to_Ground": 0}
            ThermalSettings.updateParams(**kwargs)
            encodeFace(ThermalSettings, triFace, -normal)
            surface2 = ThermalSettings.applyToIDF(idf)
            faceObjects += [surface1, surface2]
        else:
            surface1 = ThermalSettings.applyToIDF(idf)
            faceObjects += [surface1]

    return faceObjects
