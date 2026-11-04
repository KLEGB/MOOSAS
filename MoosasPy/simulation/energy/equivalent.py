"""Optional exact thermal aggregation of tessellation pieces, after forcing."""
from dataclasses import replace
from hashlib import sha256
import json
import math
import numpy as np


def coalesce_energy_request(request):
    """Combine surfaces with identical solver fields and hourly forcing.

    This changes only the solver request. Native model geometry is untouched.
    Membership is retained in SurfaceRequest.sources. No rounded comparisons
    or approximate normals are used. Surface-level results represent groups.
    """
    zones, forcing = [], []
    before = after = 0
    for zone, irradiance in zip(request.zones, request.irradiance):
        groups = {}
        for index, surface in enumerate(zone.surfaces):
            native = surface.native_model()
            for key in ('name', 'source_element_id', 'area_m2'):
                native.pop(key, None)
            key = (json.dumps(native, sort_keys=True, allow_nan=False),
                   bool(surface.wind_driven_film),
                   np.asarray(irradiance[index], dtype='<f8').tobytes())
            groups.setdefault(key, []).append(index)
        surfaces, rows = [], []
        for indices in groups.values():
            first = zone.surfaces[indices[0]]
            if len(indices) == 1:
                combined = first
            else:
                ids = tuple(zone.surfaces[i].surface_id for i in indices)
                identity = sha256(json.dumps(ids).encode()).hexdigest()[:24]
                combined = replace(first, surface_id='equivalent:' + identity,
                    area_m2=math.fsum(zone.surfaces[i].area_m2 for i in indices),
                    sources=first.sources + tuple(('equivalent_member', uid) for uid in ids),
                    radiation_geometry=())
            surfaces.append(combined)
            rows.append(irradiance[indices[0]])
        values = np.array(rows, copy=True)
        values.setflags(write=False)
        zones.append(replace(zone, surfaces=tuple(surfaces)))
        forcing.append(values)
        before += len(zone.surfaces)
        after += len(surfaces)
    return replace(request, zones=tuple(zones), irradiance=tuple(forcing),
        warnings=request.warnings + (f'Exact equivalent thermal surfaces: {before} -> {after}; physical geometry unchanged',))
