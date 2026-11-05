from ...utils import shapely
from datetime import datetime
from ...transform.geometry.geos import Projection
from ...transform.geometry.geos import planar_surface


def _mesh_to_radiance_object(geos, material, object_id):
    """
    Convert a mesh geometry to a Radiance object string representation.
    
    Parameters
    ----------
    geos : shapely.Geometry or list of shapely.Geometry
        Input geometry or list of geometries to convert.
    material : str
        Material name to assign to the Radiance object.
    id : str
        Identifier prefix for the generated polygons.
    
    Returns
    -------
    str
        Radiance-formatted string representation of the mesh geometry, including material,
        polygon identifiers, and vertex coordinates. Returns an empty string if no valid
        triangles are generated.
    """
    if isinstance(geos, shapely.Geometry):
        geos = [geos]
    geoStr = []
    for gid,geo in enumerate(geos):
        # Opaque geometry has already been triangulated on its physical plane.
        # Re-projecting a tiny valid triangle requires a second, ill-conditioned
        # basis and can erase it. Preserve its actual three-dimensional vertices.
        coordinates=shapely.get_coordinates(geo,include_z=True)
        if geo.geom_type=='Polygon' and len(coordinates)==4 and len(geo.interiors)==0:
            vertices=coordinates[:-1]
            import numpy as np
            area_vector=np.cross(vertices[1]-vertices[0],vertices[2]-vertices[0])
            if not np.isfinite(vertices).all():
                raise ValueError(f'Degenerate Radiance triangle for {object_id}')
            if not np.any(area_vector):
                # A zero-area triangulation remnant has no optical surface.
                # Retain every nonzero triangle, including very small ones.
                continue
            geoStr.append(f'{material} polygon {object_id}_{gid}_0 0 0 9')
            geoStr.extend('    '+' '.join(pt.astype(str)) for pt in vertices)
            geoStr.append('')
            continue
        try:
            proj = Projection.fromPolygon(geo)
        except IndexError as e:
            raise ValueError(f"Invalid Radiance projection for {object_id}") from e
        try:
            # Project rings separately to retain holes in the input polygon.
            rings = [proj.toUV(ring) for ring in shapely.get_rings(geo)]
            geoUV = shapely.polygons(rings[0], holes=rings[1:] or None)
            triangles = shapely.constrained_delaunay_triangles(planar_surface(geoUV))
            triangles = [proj.toWorld(tri) for tri in shapely.get_parts(triangles)]
            if len(triangles) == 0:
                continue
            for trIdx, tri in enumerate(triangles):
                pts = shapely.get_coordinates(tri, include_z=True)
                geoStr += [f"{material} polygon {object_id}_{gid}_{trIdx} 0 0 {(len(pts)-1) * 3}"]
                for pt in pts[:-1]:
                    geoStr += ["    "+" ".join(pt.astype(str))]
                geoStr += [""]
        except Exception as e:
            raise ValueError(f"Radiance geometry export failed for {object_id}") from e
    return "\n".join(geoStr) + "\n"


def _material_library():
    """
    Return a string containing Radiance material definitions for common building materials.
    
    Parameters
    ----------
    None
    
    Returns
    -------
    str
        A string defining materials (plastic and glass) in Radiance format, including default_floor,
        default_roof, default_wall with specified reflectances, and a base glazing material.
    """
    """
            Visible Light Transmittance (VLT) : Tn
        =>    void glass sketch_win 0 0 3 tn tn tn
        =>    tn =  (Math.sqrt(0.8402528435+0.0072522239*Tn*Tn)-0.9166530661)/0.0036261119/Tn
        => VLT : 0.737, tn = 0.803
        => VLT : 0.803, tn = 0.874
        => VLT : 0.915, tn = 0.996
    """
    matStr = """
####Materials
void plastic default_floor
0
0
5 0.3 0.3 0.3 0 0
void plastic default_roof
0
0
5 0.75 0.75 0.75 0 0
void plastic default_wall
0
0
5 0.6 0.6 0.6 0 0
void glass glazing_
0
0
3 0.78 0.78 0.78

####Materials
"""
    return matStr


def _get_sky(date: datetime, sky_type, lat, lon, diffuse_illuminance=10000, utc_offset_hours=0):
    """
    Generate a Radiance sky description string for a given date and location.
    
    Parameters
    ----------
    date : datetime
        The date and time for which the sky is generated, used to determine month, day, and hour.
    skyType : str
        Type of sky model to generate (e.g., "-c" for cloudy, other values for different sky types).
    lat : float or str
        Latitude of the location in degrees, used in the sky generation command.
    lon : float or str
        Longitude of the location in degrees, used in the sky generation command.
    diff : float, optional
        Diffuse solar irradiance value (in W/m虏). Used only if skyType is "-c". Default is 10000.
    
    Returns
    -------
    str
        A formatted string containing the Radiance sky definition commands, including gensky command
        and associated glow and source elements for sky and ground.
    """
    hour = date.hour + date.minute / 60 + date.second / 3600
    if date.utcoffset() is not None:
        utc_offset_hours = date.utcoffset().total_seconds() / 3600
    skyStr = f"!gensky {date.month} {date.day} {hour} {sky_type} -a {lat} -o {-float(lon)} -m {-15 * utc_offset_hours} -g 0.200"
    if sky_type == "-c":
        skyStr += f" -B {diffuse_illuminance / 179.0}"
    skyStr += "\n"
    skyStr += """skyfunc glow sky_mat
0
0
4
    1 1 1 0
sky_mat source sky
0
0
4
    0 0 1 180
skyfunc glow ground_glow
0
0
4
    1 .8 .5 0
ground_glow source ground
0
0
4
    0 0 -1 180"""
    return skyStr


def _visible_material(settings, role, glass, name, black=False, split_transport=False):
    """Export physical normal-incidence VLT as Radiance glass transmissivity."""
    import math
    key = 'visible_transmittance' if glass else 'visible_reflectance'
    default = .7 if glass else {'floor': .3, 'ceiling': .75}.get(role, .6)
    value = float(settings.get(key, default))
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f'{name}: {key} must be in [0,1]')
    record = {key: value, 'assumed': key not in settings}
    if glass and value > 0:
        # Default glass refractive index is 1.52; even lossless glass reflects.
        tn = (math.sqrt(.8402528435 + .0072522239 * value * value) - .9166530661) / (.0036261119 * value)
        if tn > 1 + 1e-6:
            raise ValueError(f'{name}: VLT exceeds clear glass limit (~0.918); complex optics are unsupported')
        material = f'void glass {name}\n0\n0\n3 {tn} {tn} {tn}\n'
    else:
        reflectance = 0 if black or glass else value
        green = 0 if split_transport else reflectance
        material = f'void plastic {name}\n0\n0\n5 {reflectance} {green} {reflectance} 0 0\n'
    return material, record
