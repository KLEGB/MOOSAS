from .radiance import _get_sky, _material_library, _mesh_to_radiance_object
from ...model import MoosasModel
from ...transform.geometry.element import MoosasElement, MoosasSpace
from ...transform.geometry.geos import Projection, Vector
from ...transform.geometry.grid import MoosasGrid
from ...utils import np, shapely, path, os,mixItemListToList
from datetime import datetime


def _generate_radiance_geometry(roof, floor, others):
    """
    Generate a Radiance geometry string from roof, floor, and other building elements.
    
    Parameters
    ----------
    roof : list of MeshFace
        List of mesh faces representing the roof elements.
    floor : list of MeshFace
        List of mesh faces representing the floor elements.
    others : list of MeshFace
        List of mesh faces representing other elements (e.g., walls, glazing).
        The category attribute of each face determines its treatment:
        categories 0/3/4/-1 for opaque faces and 1/5/6 for glazing.
        Air boundaries (2) and ignored faces (-2) are omitted.
    
    Returns
    -------
    str
        A string containing the Radiance-formatted geometry representation.
    """
    geoStr = ''
    ids = 0
    for moFace in floor:
        geoStr += _mesh_to_radiance_object(_triangulate_opaque(moFace), "default_floor", ids)
        ids += 1
    for moFace in roof:
        geoStr += _mesh_to_radiance_object(_triangulate_opaque(moFace), "default_roof", ids)
        ids += 1
    for moFace in others:
        if moFace.category in (0, 3, 4, -1):
            geoStr += _mesh_to_radiance_object(_triangulate_opaque(moFace), "default_wall", ids)
        if moFace.category in (1, 5, 6):
            geoStr += _mesh_to_radiance_object(moFace.representation(), "glazing_", ids)
        ids += 1
    return geoStr


def _model_to_radiance(model: MoosasModel, date: datetime, sky_type, lat, lon, diffuse_illuminance=10000,
                       rad_path=rf"{path.libDir}\rad\model.rad"):
    """
    Convert a MoosasModel to a Radiance input file (.rad) string and write it to disk.
    
    Parameters
    ----------
    model : MoosasModel
        The building model containing spaces, walls, glazing, and other geometry.
    date : datetime
        The date and time for which the sky conditions are generated.
    skyType : object
        Specifies the type of sky (e.g., sunny, cloudy) for Radiance sky generation.
    lat : float or int
        Latitude of the site in degrees, used for solar position calculation.
    lon : float or int
        Longitude of the site in degrees, used for solar position calculation.
    diff : int, optional
        Diffuse solar irradiance value (in W/m虏). Default is 10000.
    radPath : str, optional
        File path where the generated .rad file will be saved. Default is a path within `path.libDir`.
    
    Returns
    -------
    str
        The complete Radiance input string containing sky, materials, and geometry definitions.
    """
    roof, floor, others = [], [], []
    for spc in model.spaceList:
        faces = spc.getAllFaces(to_dict=True)
        ceils, ground = [], []
        for moface in faces["MoosasCeiling"]:
            ceils = np.append(ceils, moface)
        for moface in faces["MoosasFloor"]:
            ground = np.append(ground, moface)
        roof = np.append(roof, ceils)
        floor = np.append(floor, ground)
    roof = list(set(roof).difference(set(floor)))
    floor = list(floor)
    others = list(model.wallList)+list(model.glazingList)+list(model.skylightList)
    # roof = [model.geoId.index(item) for item in roof]
    # floor = [model.geoId.index(item) for item in floor]
    # others = [model.geoId.index(item) for item in others]
    # roof = np.array(model.geometryList)[roof]
    # floor = np.array(model.geometryList)[floor]
    # others = np.array(model.geometryList)[others]

    geoStr = _generate_radiance_geometry(roof, floor, others)
    radStr = _get_sky(date, sky_type, lat, lon, diffuse_illuminance) + _material_library() + geoStr
    with open(rad_path, 'w+') as f:
        f.write(radStr)
    return radStr

def _triangulate_opaque(moFace:MoosasElement)->list[shapely.Geometry]:
    """
    Compute triangulated opaque geometry from a MoosasElement face.
    
    Parameters
    ----------
    moFace : MoosasElement
        The input MoosasElement containing the face and glazing elements. The face is used to generate base geometry,
        and its normal is used for projection. Glazing elements are treated as holes in the base face.
    
    Returns
    -------
    list[shapely.Geometry]
        A list of shapely Geometry objects representing the triangulated opaque regions in world coordinates,
        with glazing areas subtracted as holes and projected back from UV to 3D space.
    """
    proj = Projection(origin=np.mean(shapely.get_coordinates(moFace.face, include_z=True), axis=0), unitZ=moFace.normal)
    baseFace = mixItemListToList(moFace.face)
    baseBrep,holes=[],[]
    for face in baseFace:
        baseBrep.append(shapely.get_exterior_ring(face))
        if len(shapely.get_rings(face))>1:
            holes+=list(shapely.get_rings(face))[1:]
    for gls in moFace.glazingElement:
        holes.append(gls.representation())
    baseBrep = [shapely.polygons(proj.toUV(face)) for face in baseBrep]
    baseBrep = shapely.union_all(baseBrep)
    holes = [proj.toUV(face) for face in holes]
    for h in holes:
        if shapely.get_dimensions(h) == 1:
            h = shapely.polygons(h)
        baseBrep = shapely.difference(baseBrep,h)
    # Constrain triangles to the boundary, including concavities and window holes.
    baseBrep = shapely.constrained_delaunay_triangles(baseBrep)
    baseBrep = [proj.toWorld(tri) for tri in shapely.get_parts(baseBrep)]
    return baseBrep
def _space_to_radiance(space: MoosasSpace, date: datetime, sky_type, lat, lon, diffuse_illuminance=10000,
                       rad_path=rf"{path.libDir}\rad\model.rad"):
    """
    Generate a Radiance input file string and write it to disk based on the provided space geometry and environmental conditions.
    
    Parameters
    ----------
    space : MoosasSpace
        The space object containing the 3D geometry, from which faces are extracted.
    date : datetime
        The date and time for which the sky conditions are computed.
    skyType : object
        Specifies the type of sky model to use (e.g., sunny or cloudy).
    lat : float or int
        Latitude of the location, used in sky calculation.
    lon : float or int
        Longitude of the location, used in sky calculation.
    diff : int, optional
        Diffuse solar radiation value (in Wh/m虏), default is 10000.
    radPath : str, optional
        File path where the Radiance script will be saved. Defaults to a path in `path.libDir`.
    
    Returns
    -------
    str
        The complete Radiance input string, including sky, materials, and geometry definitions.
    """
    roof, floor, others = [], [], []
    faces = space.getAllFaces(to_dict=True)
    for moface in faces["MoosasCeiling"]:
        roof = np.append(roof, moface)
    for moface in faces["MoosasFloor"]:
        floor = np.append(floor, moface)
    for moface in faces["MoosasWall"]:
        others = np.append(others, moface)
    for moface in faces["MoosasGlazing"]:
        others = np.append(others, moface)
    for moface in faces["MoosasSkylight"]:
        others = np.append(others, moface)

    geoStr = _generate_radiance_geometry(roof, floor, others)

    radStr = _get_sky(date, sky_type, lat, lon, diffuse_illuminance) + _material_library() + geoStr
    with open(rad_path, 'w+') as f:
        f.write(radStr)
    return radStr


def _write_grid(element: MoosasElement, grid_path=rf"{path.libDir}\rad\grid.input", normal=None, append=True):
    """
    Write grid points and their normal vectors to a file.
    
    Parameters
    ----------
    element : MoosasElement
        The element used to generate the grid.
    gridPath : str, optional
        Path to the output file where grid data will be written. Default is constructed using `path.libDir`.
    normal : array-like or Vector, optional
        Normal vector to be written with each point; if None, uses the grid's default normal. Default is None.
    append : bool, optional
        If True, appends to the file; otherwise, overwrites it. Default is True.
    
    Returns
    -------
    list of str
        List of formatted strings representing grid points and normals written to the file.
    """
    if append:
        mode = 'a+'
    else:
        mode = 'w+'
    gridStr = []
    grid = MoosasGrid(element)
    for pts in grid.gridPoints:
        pts = shapely.get_coordinates(pts, include_z=True).astype(str)[0]
        nor = grid.normal.astype(str) if normal is None else Vector(normal).array.astype(str)

        gridStr += [" ".join(pts) + " " + " ".join(nor)]

    with open(grid_path, mode) as f:
        f.write('\n'.join(gridStr) + "\n")

    return gridStr
