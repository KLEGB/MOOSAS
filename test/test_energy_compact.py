from types import SimpleNamespace
from pathlib import Path
import numpy as np
import pytest

from MoosasPy.simulation.energy.input import SurfaceRequest,ZoneRequest,EnergyRequest
from MoosasPy.simulation.energy.input import compact_adiabatic_surfaces
from MoosasPy.simulation.energy.runner import EnergyRunner
from MoosasPy.utils.tools import path,native_platform


def test_almost_horizontal_energy_frame_preserves_true_surface_area():
    import shapely
    from MoosasPy.simulation.energy.input import _surface_projection,_radiation_geometry
    n=np.array([.07948625544884542,-.000028986212476656,.9968359616077865])
    n/=np.linalg.norm(n)
    u=np.cross([0.,0.,1.],n);u/=np.linalg.norm(u)
    v=np.cross(n,u)
    origin=np.array([40.,38.,19.5])
    polygon=shapely.Polygon([origin,origin+2*u,origin+2*u+4*v,origin+4*v])
    projection=_surface_projection(origin,n)
    uv=projection.toUV(polygon)
    assert uv.area==pytest.approx(8.,abs=1e-10)
    assert np.allclose(projection.rotateMatrix.T@projection.rotateMatrix,np.eye(3),atol=1e-12)
    restored=_radiation_geometry(uv,projection)[0][0]
    assert np.allclose(restored,shapely.get_coordinates(polygon,include_z=True),atol=1e-11)


def request(nonzero_adiabatic=False):
    surfaces=tuple(SurfaceRequest(surface_id=name,source_element_id=name,zone_id='room',role='wall',
        boundary_condition=boundary,area_m2=area,resistance_m2k_w=.3,thermal_mass_j_m2k=60000.,
        reflectance=.6,transmittance=0.,emissivity=.9,inside_emissivity=.9,hi_w_m2k=3.,
        ho_w_m2k=0. if boundary=='adiabatic' else 12.,sky_view=0. if boundary=='adiabatic' else .5,
        ground_view=0. if boundary=='adiabatic' else .5,ground_contact_w_m2k=0.,normal=(1.,0.,0.))
        for name,area,boundary in [('a',2.,'adiabatic'),('b',3.,'adiabatic'),('c',4.,'outdoors')])
    full=lambda value:np.full(8760,value,dtype=float)
    zone=ZoneRequest(zone_id='room',name='room',area_m2=4.,volume_m3=12.,surfaces=surfaces,
        cooling_cop=3.,heating_cop=3.,initial_c=20.,internal_radiant_fraction=.6,solar_floor_fraction=.642,
        internal_w=full(100),lighting_w=full(0),equipment_w=full(0),heat_setpoint_c=full(20),
        cool_setpoint_c=full(26),ventilation_m3_h=full(20),hvac_enabled=full(1))
    weather=SimpleNamespace(temperature=full(15),ground_temperature=full(18),sky_temperature=full(285),
        pressure=full(101325),wind_speed=full(0))
    incident=np.zeros((3,8760));incident[2,:]=500
    if nonzero_adiabatic:incident[1,120]=.3
    return EnergyRequest((zone,),weather,(incident,),hours=24)


def test_compaction_preserves_area_and_exterior_receiving_surface():
    original=request();compact,lineage=compact_adiabatic_surfaces(original)
    assert len(original.zones[0].surfaces)==3 and len(compact.zones[0].surfaces)==2
    assert sum(s.area_m2 for s in compact.zones[0].surfaces)==pytest.approx(9)
    assert compact.zones[0].surfaces[-1] is original.zones[0].surfaces[-1]
    assert sum(part['area_m2'] for parts in lineage.values() for part in parts)==pytest.approx(5)


def test_compaction_never_merges_different_hourly_forcing():
    original=request(nonzero_adiabatic=True);compact,lineage=compact_adiabatic_surfaces(original)
    assert len(compact.zones[0].surfaces)==3
    assert not lineage
    np.testing.assert_array_equal(original.irradiance[0],compact.irradiance[0])


def test_compaction_matches_stock_native_energy():
    platform,suffix=native_platform()
    if not (Path(path.libDir)/'moosas_energy'/platform/'bin'/('MoosasEnergy'+suffix)).exists():
        pytest.skip('Native energy executable unavailable on this platform')
    original=request();compact,_=compact_adiabatic_surfaces(original)
    a=EnergyRunner(energy_input=original,preheat=0).run()
    b=EnergyRunner(energy_input=compact,preheat=0).run()
    np.testing.assert_allclose(a.zone_hourly_energy_kwh,b.zone_hourly_energy_kwh,atol=1e-9,rtol=1e-9)
    np.testing.assert_allclose(a.zone_temperature_c,b.zone_temperature_c,atol=1e-9,rtol=1e-9)
