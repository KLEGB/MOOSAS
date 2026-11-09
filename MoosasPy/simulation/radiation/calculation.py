from __future__ import annotations

import tempfile

from ...transform.importers.geo import writeGeo
from ...transform.geometry.grid import MoosasGrid
from ...model.space import MoosasSpace
from ...transform.geometry.element import MoosasSkylight, MoosasGlazing, MoosasElement
from ...transform.geometry.geos import Ray, Vector
from ...utils import np, os, Iterable
from ...utils import path
from ...utils.tools import native_platform
from ...utils.constant import rad
from ..runner import Runner


def _default_sky_positions():
    sky_path = os.path.join(path.dataBaseDir, "radiation", "sun_position.csv")
    with open(sky_path, encoding="utf-8") as sky_file:
        return [
            Vector(np.array(line.split(","), dtype=float))
            for line in sky_file.read().splitlines()
            if line.strip()
        ]


def calculate_model_radiation(model, cumulative_sky, reflection=1):
    """
    Calculate radiation for each space in the model using a fast single-call method via MoosasRad.exe.
    
    Parameters
    ----------
    model : object
        The building model containing spaces, geometry, and sky data. Must have `spaceList`, 
        `cumSky['summer']`, `cumSky['winter']`, and settings attributes.
    reflection : int, optional
        The number of reflections to consider in the radiation calculation. Default is 1.
    
    Returns
    -------
    object
        The input model with updated space settings including 'zone_summerrad' and 'zone_winterrad'
        values representing the total solar radiation for summer and winter periods.
    """
    model.x = 1000
    """
         This method is faster than per-space calculation since it calls MoosasRad.exe once.
         Radiation Calculation in MoosasRad is parallel.
    """
    """construct rays"""
    rays, windowArea, spaceIdx = [], [], []
    for i, space in enumerate(model.spaceList):
        moFaces = space.getAllFaces(to_dict=False)
        for moGeometry in moFaces:
            if isinstance(moGeometry, MoosasSkylight) or isinstance(moGeometry, MoosasGlazing):
                rays.append(Ray(
                    origin=Vector(moGeometry.getWeightCenter()),
                    direction=Vector(moGeometry.normal)
                ))
                windowArea.append(moGeometry.area3d())
                spaceIdx.append(i)

    rays = np.array(rays)
    windowArea = np.array(windowArea)
    spaceIdx = np.array(spaceIdx)

    """radiation calculation"""
    with tempfile.TemporaryDirectory(prefix="moosas-radiation-") as work_dir:
        geo_path = write_radiation_geometry(model, work_dir=work_dir)
        factors = _calculate_position_radiation_factors(
            position_ray=rays,
            sky_positions=cumulative_sky['summer'].positions,
            geo_path=geo_path,
            reflection=reflection,
        )
    summerRad = factors @ cumulative_sky['summer'].values
    winterRad = factors @ cumulative_sky['winter'].values

    """sum up space radiation"""
    for i, space in enumerate(model.spaceList):
        subList = [idx for idx in range(len(spaceIdx)) if spaceIdx[idx] == i]
        space.settings['zone_summerrad'] = np.sum(
            rad.DEFAULT_SHGC * windowArea[subList] * summerRad[subList])
        space.settings['zone_winterrad'] = np.sum(
            rad.DEFAULT_SHGC * windowArea[subList] * winterRad[subList])

    return model


def calculate_space_radiation(space: MoosasSpace, cumulative_sky, reflection=1) -> dict:
    """
    Calculate seasonal radiation for a space by aggregating aperture-level radiation contributions.
    
    Parameters
    ----------
    space : MoosasSpace
        The space object containing apertures (skylights or glazing) for which radiation is calculated.
    reflection : float, optional
        Reflection coefficient used in radiation calculation. Default is 1.
    
    Returns
    -------
    dict
        Updated settings dictionary of the space with 'zone_summerrad' and 'zone_winterrad' keys 
        representing total summer and winter radiation values weighted by window areas.
    """
    """
        This method packing up the ray of all apertures of the space and call MoosasRad.exe once.
        Radiation Calculation in MoosasRad is parallel.
    """
    settings = space.settings
    model = space.parent
    moFaces = space.getAllFaces(to_dict=False)
    rays = []
    windowArea = []
    for moGeometry in moFaces:
        if isinstance(moGeometry, MoosasSkylight) or isinstance(moGeometry, MoosasGlazing):
            rays.append(Ray(
                origin=Vector(moGeometry.getWeightCenter()),
                direction=Vector(moGeometry.normal)
            ))
            windowArea.append(moGeometry.area3d())
    windowArea = np.array(windowArea)
    with tempfile.TemporaryDirectory(prefix="moosas-radiation-") as work_dir:
        geo_path = write_radiation_geometry(model, work_dir=work_dir)
        factors = _calculate_position_radiation_factors(
            position_ray=rays,
            sky_positions=cumulative_sky['summer'].positions,
            geo_path=geo_path,
            reflection=reflection,
        )
    settings['zone_summerrad'] = np.sum(windowArea * (factors @ cumulative_sky['summer'].values))
    settings['zone_winterrad'] = np.sum(windowArea * (factors @ cumulative_sky['winter'].values))

    return settings


def calculate_face_radiation(face: MoosasElement, grid_size=None, grid_offset=0.78, sky=None, reflection=1,
                             geo_path=None) -> np.ndarray:
    """
    Calculate the sky visibility for each patch in the sky model, for a gridded face.
    the return result would be the average sky visibility, not for a position but the whole grid.

    Parameters
    ----------
    face : object
        The face to be calculated.
    sky : CumulativeSky-compatible object
        The position will be replaced if a sky is given,
        and the radiation value will be multiplied by the visibility.
    gridSize : float
        The calculation grid size in meters. (default 1.0)
    gridOffset : float
        The grid offset in meters. (default 0.2)
    reflection : float
        How many reflection in the ray test calculation. (default 0)
    geo_path : str
        The path of the geometric model.
    Returns
    -------
    np.ndarry
        A numpy array containing average sky patches visibility (len=145).
    """
    model = face.parent
    if geo_path is None:
        with tempfile.TemporaryDirectory(prefix="moosas-radiation-") as work_dir:
            temporary_geo_path = write_radiation_geometry(model, work_dir=work_dir)
            return calculate_face_radiation(
                face,
                grid_size=grid_size,
                grid_offset=grid_offset,
                sky=sky,
                reflection=reflection,
                geo_path=temporary_geo_path,
            )

    grid: MoosasGrid = MoosasGrid(face, grid_size, grid_offset)
    if sky is None:
        position = _default_sky_positions()
    else:
        position = sky.positions
    rays = []
    fixMatrix = []
    allCells = [c for row in grid.gridCell for c in row if c.valid]
    if len(allCells) == 0:
        return np.zeros(len(position))
    world_origins = [Vector(point.coords[0]) for point in grid.gridPoints]
    for c, world_origin in zip(allCells, world_origins):
        thisRays = [Ray(world_origin, pos,value=1.0) for pos in position]
        rays += thisRays
        fixMatrix.append(np.array([abs(Vector.dot(c.direction, r.direction)) for r in thisRays]))

    unHit = np.arange(len(rays))
    rays = np.array(rays)
    while reflection >= 0 and len(rays[unHit]) > 0:
        newRays = ray_test(rays[unHit], geo_path=geo_path)
        if len(newRays) != len(unHit):
            raise Exception('Ray test error: input and output dont have the same len')
        unHitNext = []
        for i, thisRay in enumerate(newRays):
            if thisRay is not None:
                if reflection > 0:
                    thisRay.value = rays[unHit[i]].value * rad.CONTENT_REFLECTION
                    rays[unHit[i]] = thisRay
                    unHitNext.append(unHit[i])
                else:
                    rays[unHit[i]].value = 0
        unHit = unHitNext
        if len(rays[unHit]) == 0:
            break
        reflection -= 1

    rays = np.array([ra.value for ra in rays])
    rays = rays.reshape(len(allCells), int(len(rays) / len(allCells)))
    if sky is None:
        rays = [cell * fixMatrix[i] for i, cell in enumerate(rays)]
    else:
        rays = [cell * sky.values * fixMatrix[i] for i, cell in enumerate(rays)]
    return np.mean(rays, axis=0)


def calculate_position_radiation(position_ray: Ray | Iterable[Ray], sky,
                                 model=None, reflection=1, geo_path=None) -> Iterable[float]:
    """
        Cumulative radiation for positions with factors.
        The position are defined as Ray class with origins and directions.
        list or ndarry or Ray can be given as positionRay.
        The return value is unit in kWh/m2
        Model or geoPath should be provided.

        Parameters
        -------------------------------------

        positionRay: Iterable[Ray] position(origin, factor) to test. Put as much as possible in one coll on this func.
        sky: cumulative sky model used in this function.
        model: MoosasModel the reflectance test content.
        reflection: how many reflection will be calculated. default 1
        geoPath: optional *.geo file input for the test content.

        Returns
        -------
        Iterable[float]
            The return value is unit in kWh/m2
    """
    factors = _calculate_position_radiation_factors(
        position_ray,
        sky.positions,
        model=model,
        reflection=reflection,
        geo_path=geo_path,
    )
    return factors @ sky.values


def _calculate_position_radiation_factors(position_ray: Ray | Iterable[Ray], sky_positions,
                                          model=None, reflection=1, geo_path=None) -> np.ndarray:
    if isinstance(position_ray, Ray):
        position_ray = np.array([position_ray])
    elif isinstance(position_ray, list):
        position_ray = np.array(position_ray)
    elif not isinstance(position_ray, np.ndarray):
        raise TypeError(f'Expected {list}, {np.ndarray}, or {Ray}; got {type(position_ray)}')
    if len(position_ray) == 0:
        return np.empty((0, len(sky_positions)))
    if geo_path is None:
        if model is None:
            raise Exception('Geo export error: empty model.')
        with tempfile.TemporaryDirectory(prefix="moosas-radiation-") as work_dir:
            temporary_geo_path = write_radiation_geometry(model, work_dir=work_dir)
            return _calculate_position_radiation_factors(
                position_ray,
                sky_positions,
                reflection=reflection,
                geo_path=temporary_geo_path,
            )

    rays = []
    for pointRay in position_ray:
        thisRays = [
            Ray(
                pointRay.origin,
                position,
                value=max(Vector.dot(position, pointRay.direction), 0),
            )
            for position in sky_positions
        ]
        nagativeRays = [
            Ray(
                pointRay.origin,
                Vector(-position.array),
                value=rad.GROUND_REFLECTION
                * max(Vector.dot(Vector(-position.array), pointRay.direction), 0),
            )
            for position in sky_positions
        ]
        thisRays += nagativeRays
        rays += thisRays
    # whether the ray hit anything
    unHit = np.arange(len(rays))
    rays = np.array(rays)
    while reflection >= 0 and len(rays[unHit]) > 0:
        newRays = ray_test(rays[unHit], geo_path=geo_path)
        if len(newRays) != len(unHit):
            raise Exception('Ray test error: input and output dont have the same len')
        unHitNext = []
        for i, thisRay in enumerate(newRays):
            if thisRay is not None:
                if reflection > 0:
                    thisRay.value = rays[unHit[i]].value * rad.CONTENT_REFLECTION
                    rays[unHit[i]] = thisRay
                    unHitNext.append(unHit[i])
                else:
                    rays[unHit[i]].value = 0
        unHit = unHitNext
        if len(rays[unHit]) == 0:
            break
        reflection -= 1

    rays = np.array([ra.value for ra in rays])
    rays = rays.reshape(len(position_ray), 2, len(sky_positions))
    return np.sum(rays, axis=1)


def ray_test(rays: Iterable[Ray], model=None, geo_path: str = None,
             ray_path: str = None) -> list[Ray | None]:
    """
        call MoosasRad.exe to test the ray face intersection and reflection.
        if the ray hit a face: result ray will be the reflection ray of the input ray.
        if the ray doesnt hit a face: result==None.
        Model or geoPath should be provided.

        -------------------------------------

        rays: Iterable[Ray] ray to test. Put as much as possible rays in one coll on this func.
        model: MoosasModel the reflectance test content.
        geoPath: optional *.geo file input for the test content.
        ray_path: option temp path for the ray file.

        returns: Iterable[Ray | None]
        if the intersection is valid, the reflection of the ray will be return.
        If not, None will be add.
    """
    # export geometry file or use exist file directly
    if geo_path is None:
        if model is None:
            raise Exception('empty model')
        with tempfile.TemporaryDirectory(prefix="moosas-radiation-") as work_dir:
            temporary_geo_path = write_radiation_geometry(model, work_dir=work_dir)
            return ray_test(rays, geo_path=temporary_geo_path, ray_path=ray_path)
    temporary_directory = tempfile.TemporaryDirectory(prefix="moosas-ray-") if ray_path is None else None
    try:
        if ray_path is None:
            ray_path = os.path.join(temporary_directory.name, "rays.i")
        else:
            ray_path = os.path.abspath(ray_path)
        result_path = os.path.join(os.path.dirname(ray_path), "rays.o")

        lines = []
        for ra in rays:
            if not isinstance(ra, Ray):
                raise Exception(f'expect{Vector},got{type(ra)}')
            lines.append(ra.dump())

        path.checkBuildDir(ray_path, result_path)
        with open(ray_path, 'w') as f:
            f.write('\n'.join(lines) + '\n')

        platform_dir, suffix = native_platform()
        command = [
            os.path.join(path.libDir, 'moosas_rad', platform_dir, 'bin', f'MoosasRad{suffix}'),
            '-g', geo_path,
            '-o', result_path,
            ray_path
        ]
        Runner().run_command(command)

        with open(result_path, 'r') as f:
            result = f.read().split('\n')
        reflectionRay = []
        for res in result:
            if len(res) > 6:
                res = np.array(res.split(',')).astype(float)
                res = Ray(Vector(res[:3]), Vector(res[3:]))
                if Vector.equal(res.origin, Vector(np.array([-1, -1, -1]))):
                    res = None
                reflectionRay.append(res)
        return reflectionRay
    finally:
        if temporary_directory is not None:
            temporary_directory.cleanup()


def write_radiation_geometry(model, work_dir=None):
    """
    Write a geometry file for the given model towards MoosasRayTest.exe.
    
    Parameters
    ----------
    model : object
        The geometric model to be written to the .geo file. The exact type depends on the expected input of `writeGeo`, typically representing a 3D scene or geometry structure.
    
    Returns
    -------
    str
        The absolute file path to the generated .geo file.
    """
    if work_dir is not None:
        os.makedirs(work_dir, exist_ok=True)
    geo_dir = tempfile.mkdtemp(prefix="moosas-ray-", dir=work_dir)
    geo_path = os.path.join(geo_dir, "model.geo")
    from ...transform.geometry.polygon import triangulate_planar_face
    from ...utils import shapely
    # MoosasRad's projected-ring reader is ill-conditioned for nearly
    # vertical holes. Export physical triangles, with the holes already
    # removed in each face's own 3D frame, rather than altering the engine.
    lines, numerical_fragments = [], []
    referenced = {str(geometry.faceId) for element in model.getAllFaces()
                  for geometry in element.geometry}
    for geometry in model.geometryList:
        if str(geometry.faceId) not in referenced:
            continue
        if geometry.category in (2, -2):
            continue
        for face in np.asarray(geometry.face,dtype=object).reshape(-1):
            for index, triangle in enumerate(triangulate_planar_face(face)):
                xyz = shapely.get_coordinates(triangle,include_z=True)
                normal = np.cross(xyz[1]-xyz[0],xyz[2]-xyz[0])
                area = float(np.linalg.norm(normal))/2
                edges = np.linalg.norm(np.roll(xyz[:3],-1,axis=0)-xyz[:3],axis=1)
                if np.min(edges) <= 1e-12 or area <= 1e-12:
                    numerical_fragments.append(dict(face_id=geometry.faceId,area_m2=area,
                        reason='floating point triangulation remnant below native ring resolution'))
                    continue
                normal /= 2*area
                lines.extend((f'f,{geometry.category},{geometry.faceId}_{index}',
                              'fn,'+','.join(map(str,normal))))
                lines.extend('fv,'+','.join(map(str,p)) for p in xyz)
                lines.append(';')
    with open(geo_path,'w',encoding='ascii') as stream:
        stream.write('\n'.join(lines)+'\n')
    import json
    with open(geo_path+'.audit.json','w',encoding='utf-8') as stream:
        json.dump(dict(numerical_fragments=numerical_fragments,
            omitted_numerical_area_m2=sum(p['area_m2'] for p in numerical_fragments)),stream,indent=2)
    # with open(geoPath,'a') as f:
    #    f.write(
    #        '''
    #        Face1 99999
    #        Normal
    #        0 0 1
    #        Vertices
    #        -99999.0 -99999.0 0.0
    #        -99999.0 99999.0 0.0
    #        99999.0 99999.0 0.0
    #        99999.0 -99999.0 0.0
    #
    #        '''
    #    )
    return geo_path
