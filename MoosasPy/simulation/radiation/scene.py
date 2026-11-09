from .radiance import _get_sky as _raw_get_sky, _material_library, _mesh_to_radiance_object
from ...model import MoosasModel
from ...model.space import MoosasSpace
from ...transform.geometry.element import MoosasElement
from ...transform.geometry.geos import Projection, Vector
from ...transform.geometry.grid import MoosasGrid
from ...utils import np, shapely, mixItemListToList
from datetime import datetime
import warnings

def _get_sky(*args,**kwargs):
    """Expand the sky command using the packaged native engine."""
    import shlex
    from pathlib import Path
    from ...utils.tools import native_platform, path
    from ..runner import Runner
    text=_raw_get_sky(*args,**kwargs)
    first,rest=text.split('\n',1)
    command=shlex.split(first[1:])
    platform_dir, suffix = native_platform()
    executable = Path(path.libDir) / 'radiance' / platform_dir / 'bin' / (command[0] + suffix)
    sky=Runner().run_command([str(executable), *command[1:]])
    if not sky.stdout.strip():raise RuntimeError('Sky generator returned empty output')
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
            material='glazing_'
            if 'visible_transmittance' in moFace.settings:
                from .radiance import _visible_material
                material=f'glazing_{ids}'
                optical, _ = _visible_material(moFace.settings, 'window', True, material)
                geoStr += optical
            geoStr += _mesh_to_radiance_object(moFace.face, material, ids)
        ids += 1
    return geoStr


def _model_to_radiance(model: MoosasModel, date: datetime, sky_type, lat, lon, diffuse_illuminance: float = 10000,
                       *, rad_path: str, utc_offset_hours: float = 0):
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
    rad_path : str
        Explicit output path owned by the caller's simulation workspace.
    
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
    from ...transform.geometry.polygon import planar_face_region, planar_surface
    result = []
    for face in mixItemListToList(moFace.face):
        origin,u,v,normal,region = planar_face_region(face)
        if region.is_empty:
            continue
        for glazing in moFace.glazingElement:
            for aperture in mixItemListToList(glazing.face):
                xyz=shapely.get_coordinates(aperture,include_z=True)
                if np.max(np.abs((xyz-origin)@normal)) > .001:
                    continue  # Opening belongs to another physical facet.
                _,_,_,_,opening_region = planar_face_region(aperture)
                if opening_region.is_empty:
                    continue
                rings=[]
                for ring in shapely.get_rings(aperture):
                    delta=shapely.get_coordinates(ring,include_z=True)-origin
                    rings.append(shapely.LinearRing(np.column_stack((delta@u,delta@v))))
                try:
                    opening=planar_surface(shapely.Polygon(rings[0],rings[1:]))
                except ValueError as exc:
                    # Malformed imported apertures must not abort every
                    # daylight result for an otherwise usable model. Keep the
                    # host opaque patch intact and report the skipped opening;
                    # energy/AFN retain their independent geometry paths.
                    import warnings
                    warnings.warn(
                        f"Skipping invalid aperture during daylight scene build: {exc}",
                        RuntimeWarning, stacklevel=2,
                    )
                    continue
                region=region.difference(opening)
        for triangle in shapely.get_parts(shapely.constrained_delaunay_triangles(planar_surface(region))):
            xy=shapely.get_coordinates(triangle)
            result.append(shapely.Polygon(origin+xy[:,0,None]*u+xy[:,1,None]*v))
    return result
def _space_to_radiance(space: MoosasSpace, date: datetime, sky_type, lat, lon, diffuse_illuminance=10000,
                       *, rad_path: str):
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
    rad_path : str
        Explicit output path owned by the caller's simulation workspace.
    
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


def _write_grid(element: MoosasElement, grid_path: str, normal=None, append=True):
    """
    Write grid points and their normal vectors to a file.
    
    Parameters
    ----------
    element : MoosasElement
        The element used to generate the grid.
    grid_path : str
        Explicit output path owned by the caller's simulation workspace.
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
    from ...model.building_templates import space_template, surface_template_parameters
    overrides = overrides or {}
    elements = {}
    roles = {}
    template_optics = {}
    optical_sides = {}
    for space in model.spaceList:
        profile = space_template(model, space)
        faces = space.getAllFaces(to_dict=True)
        for group, group_role in [('MoosasWall', 'wall'), ('MoosasFloor', 'floor'),
                            ('MoosasCeiling', 'ceiling'), ('MoosasGlazing', 'window'),
                            ('MoosasSkylight', 'skylight'), ('InternalMass', 'wall')]:
            for element in faces.get(group, ()):
                role=element.settings.get('geometry_surface_role_by_space',{}).get(str(space.id),group_role)
                uid = str(element.Uid)
                elements[uid] = element
                roles[uid] = role
                if element.category in (2, -2):
                    continue
                glass = element.category in (1, 5, 6)
                # Transparent curtain walls may occur in the wall group.
                optical_role = ('skylight' if role in {'floor', 'ceiling', 'skylight'} else 'window') if glass else role
                key = 'visible_transmittance' if glass else 'visible_reflectance'
                if key in overrides.get(uid, {}) or key in getattr(element, 'explicit_settings', ()):
                    continue
                boundary = element.settings.get('boundary_condition', 'outdoors' if element.isOuter else 'adiabatic')
                source = surface_template_parameters(profile, optical_role, 'outdoors' if glass else boundary).get(key)
                if source is None or source['mode'] != 'constant':
                    raise ValueError(f'{space.id}/{uid}: daylight requires constant template {key}')
                value = source['value']
                if not glass and group_role == 'wall':
                    # A shared wall can have different finishes on the two
                    # room-facing sides.  Resolve the optical property per
                    # physical side using each owner space's center, just as
                    # floors and ceilings are handled below.
                    import shapely
                    xyz = shapely.get_coordinates(element.face, include_z=True)
                    normal_xyz = np.asarray(Vector(element.normal).array, dtype=float)
                    center_xyz = np.asarray(space.getWeightCenter(), dtype=float)
                    face_center = np.mean(xyz, axis=0)
                    side = 1 if float(np.dot(center_xyz - face_center, normal_xyz)) >= 0 else -1
                    sides = optical_sides.setdefault(uid, {})
                    if side in sides and sides[side] != {key: value}:
                        warnings.warn(
                            f'{uid}: conflicting visible properties on the same physical side; '
                            'keeping the first space-template value', RuntimeWarning)
                        value = sides[side][key]
                    else:
                        sides[side] = {key: value}
                    template_optics.setdefault(uid, {key: value})
                    continue
                if not glass and group_role in {'floor', 'ceiling'}:
                    inward_z = 1 if group_role == 'floor' else -1
                    side = 1 if float(element.normal[2]) * inward_z >= 0 else -1
                    sides = optical_sides.setdefault(uid, {})
                    if side in sides and sides[side] != {key: value}:
                        warnings.warn(
                            f'{uid}: conflicting visible properties on the same physical side; '
                            'keeping the first space-template value', RuntimeWarning)
                        value = sides[side][key]
                    else:
                        sides[side] = {key: value}
                    # Floor and ceiling finishes belong to opposite sides
                    # of one slab, and need not have equal reflectances.
                    template_optics.setdefault(uid, {key: value})
                    continue
                if uid in template_optics and template_optics[uid] != {key: value}:
                    raise ValueError(f'{uid}: conflicting visible properties from adjacent space templates; set an explicit optical override')
                template_optics[uid] = {key: value}
    for collection in (model.wallList, model.glazingList, model.skylightList, model.shadingList):
        for element in collection:
            elements[str(element.Uid)] = element
    unknown = set(overrides) - set(elements)
    if unknown:
        raise ValueError(f'Optical overrides reference unknown elements: {sorted(unknown)}')
    text, optics = [], []
    for index, (uid, element) in enumerate(sorted(elements.items())):
        if element.category in (2, -2):
            continue
        glass = element.category in (1, 5, 6)
        settings = {**getattr(element, 'settings', {}), **template_optics.get(uid, {}), **overrides.get(uid, {})}
        material, record = _visible_material(settings, roles.get(uid, 'wall'), glass, f'mat_{index}', black, split_transport)
        sides = optical_sides.get(uid, {})
        if len(sides) == 2 and sides[1] != sides[-1] and uid not in overrides:
            front, front_record = _visible_material({**settings, **sides[1]}, 'floor', False, f'mat_{index}_front', black, split_transport)
            back, back_record = _visible_material({**settings, **sides[-1]}, 'ceiling', False, f'mat_{index}_back', black, split_transport)
            material = front + back + f'void mixfunc mat_{index}\n4 mat_{index}_front mat_{index}_back if(Rdot,1,0) .\n0\n0\n'
            record = dict(front=front_record, back=back_record, two_sided=True)
        record.update(uid=uid, source='override' if uid in overrides else 'template' if uid in template_optics else 'settings_or_default')
        optics.append(record)
        geometry = element.face if glass else _triangulate_opaque(element)
        if not glass and not geometry:
            continue  # Fully glazed parent: its opening is exported separately.
        if len(sides) == 2:
            oriented = []
            for face in mixItemListToList(geometry):
                xyz = shapely.get_coordinates(shapely.get_exterior_ring(face), include_z=True)[:-1]
                area_vector = np.cross(xyz, np.roll(xyz, -1, axis=0)).sum(axis=0)
                oriented.append(shapely.reverse(face) if np.dot(area_vector, element.normal) < 0 else face)
            geometry = oriented
        polygons = _mesh_to_radiance_object(geometry, f'mat_{index}', index)
        if ' polygon ' not in polygons:
            raise ValueError(f'No Radiance polygons exported for element {uid}')
        text.extend((material, polygons))
    return '\n'.join(text), optics
