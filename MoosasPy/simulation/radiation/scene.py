from .radiance import _get_sky as _raw_get_sky, _material_library, _mesh_to_radiance_object
from ...model import MoosasModel
from ...transform.geometry.element import MoosasElement, MoosasSpace
from ...transform.geometry.geos import Projection, Vector
from ...transform.geometry.grid import MoosasGrid
from ...utils import np, shapely, path, os,mixItemListToList
from datetime import datetime

def _get_sky(*args,**kwargs):
    """Expand the PIT sky command; retain the raw API for annual callers."""
    import shlex,shutil,subprocess
    text=_raw_get_sky(*args,**kwargs)
    first,rest=text.split('\n',1)
    if not first.startswith('!'):return text
    command=shlex.split(first[1:]);executable=shutil.which(command[0])
    if executable is None:raise RuntimeError('Radiance sky executable missing')
    sky=subprocess.run([executable,*command[1:]],capture_output=True,text=True,check=True)
    if 'skyfunc' not in sky.stdout:raise RuntimeError('Sky generator returned no function')
    return sky.stdout+'\n'+rest



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
        geoStr += _mesh_to_radiance_object(_triangulate_opaque(moFace), "default_wall" if abs(float(moFace.normal[2]))<.5 else "default_roof", ids)
        ids += 1
    for moFace in others:
        if moFace.category in (0, 3, 4, -1):
            geoStr += _mesh_to_radiance_object(_triangulate_opaque(moFace), "default_wall", ids)
        if moFace.category in (1, 5, 6):
            vlt=getattr(moFace,'settings',{}).get('visible_transmittance')
            material='glazing_'
            if vlt is not None:
                vlt=float(vlt)
                if not 0<=vlt<=1:raise ValueError('Invalid visible transmittance')
                if vlt==0:material='default_wall'
                else:
                    tau=((.8402528435+.0072522239*vlt*vlt)**.5-.9166530661)/(.0036261119*vlt)
                    material=f'glazing_{ids}'
                    geoStr+=f'void glass {material}\n0\n0\n3 {tau} {tau} {tau}\n'
            geoStr += _mesh_to_radiance_object(moFace.face, material, ids)
        ids += 1
    return geoStr


def _model_to_radiance(model: MoosasModel, date: datetime, sky_type, lat, lon, diffuse_illuminance=10000,
                       rad_path=rf"{path.libDir}\rad\model.rad", utc_offset_hours=0):
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
    radStr = _get_sky(date, sky_type, lat, lon, diffuse_illuminance, utc_offset_hours) + "\n" + _daylight_scene(model)[0]
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
        actual=getattr(gls,'face',None)
        if actual is None:actual=gls.representation()
        holes.extend(p for p in mixItemListToList(actual) if not shapely.is_empty(p))
    from ...transform.geometry.geos import planar_surface
    baseBrep = [planar_surface(shapely.polygons(proj.toUV(face))) for face in baseBrep]
    baseBrep = shapely.union_all(baseBrep)
    holes = [proj.toUV(face) for face in holes]
    for h in holes:
        if shapely.get_dimensions(h) == 1:
            h = shapely.polygons(h)
        baseBrep = shapely.difference(shapely.normalize(baseBrep),planar_surface(h))
    # Constrain triangles to the boundary, including concavities and window holes.
    baseBrep = shapely.constrained_delaunay_triangles(planar_surface(baseBrep))
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
    points=grid.gridPoints
    if len(points)==0:
        if grid.UVFace.is_empty or grid.UVFace.area<=0:raise ValueError('Cannot sample zero-area floor')
        sample=shapely.force_3d(shapely.point_on_surface(grid.UVFace),z=grid.gridOffset)
        points=[grid.proj.toWorld(sample)]
    for pts in points:
        coordinates=shapely.get_coordinates(pts,include_z=True)[0]
        xyz=shapely.get_coordinates(element.face,include_z=True)
        if normal is not None and Vector(normal).array[2]>.999 and np.ptp(xyz[:,2])<1e-8:
            coordinates[2]=float(xyz[0,2])+grid.gridOffset
        pts=coordinates.astype(str)
        nor = grid.normal.astype(str) if normal is None else Vector(normal).array.astype(str)

        gridStr += [" ".join(pts) + " " + " ".join(nor)]

    with open(grid_path, mode) as f:
        f.write('\n'.join(gridStr) + "\n")

    return gridStr


def _daylight_scene(model, *, black=False, overrides=None, split_transport=False):
    """One sky-free optical scene shared by PIT and annual calculations.

    Canonical visible properties are intentionally independent of SHGC and
    shortwave properties. Legacy transparency/reflection have no declared
    spectral band and are not silently reinterpreted as visible properties.
    """
    from .radiance import _visible_material
    elements = {}
    roles = {}
    for space in model.spaceList:
        faces = space.getAllFaces(to_dict=True)
        for key, role in [('MoosasCeiling', 'ceiling'), ('MoosasFloor', 'floor')]:
            for element in faces[key]:
                elements[str(element.Uid)] = element
                roles[str(element.Uid)] = role
    for collection in (model.wallList, model.glazingList, model.skylightList, model.shadingList):
        for element in collection:
            elements[str(element.Uid)] = element
    overrides = overrides or {}
    unknown = set(overrides) - set(elements)
    if unknown:
        raise ValueError(f'Optical overrides reference unknown elements: {sorted(unknown)}')
    text, optics = [], []
    for index, (uid, element) in enumerate(sorted(elements.items())):
        if element.category in (2, -2):
            continue
        glass = element.category in (1, 5, 6)
        settings = {**getattr(element, 'settings', {}), **overrides.get(uid, {})}
        material, record = _visible_material(settings, roles.get(uid, 'wall'), glass, f'mat_{index}', black, split_transport)
        record.update(uid=uid, source='override' if uid in overrides else 'settings_or_default')
        optics.append(record)
        geometry = element.face if glass else _triangulate_opaque(element)
        if not glass and not geometry:
            continue  # Fully glazed parent: its opening is exported separately.
        polygons = _mesh_to_radiance_object(geometry, f'mat_{index}', index)
        if ' polygon ' not in polygons:
            raise ValueError(f'No Radiance polygons exported for element {uid}')
        text.extend((material, polygons))
    return '\n'.join(text), optics
