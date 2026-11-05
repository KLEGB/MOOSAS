"""CETUS 1.9.4 load integration through existing MOOSAS requests and RDF."""
from dataclasses import replace
from pathlib import Path
import contextlib
import importlib.util

import numpy as np
import pytest

from MoosasPy.model import MoosasModel
from MoosasPy.simulation.energy import build_energy_input, EnergyRunner
from MoosasPy.simulation.energy.runner import solve_latent_load
from MoosasPy.simulation.weather import load_epw

ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture(scope='module')
def nbuilding(tmp_path_factory):
    folder=tmp_path_factory.mktemp('nbuilding194')
    with (folder/'load.log').open('w') as f,contextlib.redirect_stdout(f):
        model=MoosasModel.load(ROOT/'test/caseFile/NBuilding.rdf')
        weather=load_epw(ROOT/'MoosasPy/db/CHN_BJ_Beijing-Nanyuan.AP.545120_TMYx.epw',folder/'weather')
    return model,weather


def test_weather_legacy_csv_units(nbuilding):
    _,w=nbuilding
    raw=np.loadtxt(w.weather_file,delimiter=',')
    np.testing.assert_allclose(w.humidity_ratio,raw[:,4]/1000,atol=0,rtol=0)
    assert 0 < w.humidity_ratio.max() < .05


def test_analytical_balance_and_drying_credit():
    args=dict(zone_c=26,outdoor_c=30,pressure_pa=101325,outdoor_humidity_ratio=.018,
        people_moisture_kg_s=.001,fresh_air_m3_s=.5,infiltration_m3_s=.1,rh_max=.6)
    a=solve_latent_load(**args)
    assert a['dehumidification_kg_s']==pytest.approx(.001+(a['fresh_dry_air_kg_s']+a['infiltration_dry_air_kg_s'])*(.018-a['target_humidity_ratio_kg_kg']))
    assert abs(a['moisture_balance_residual_kg_s'])<1e-14
    assert a['zone_rh_fraction']==pytest.approx(.6)
    b=solve_latent_load(**dict(args,outdoor_humidity_ratio=.002))
    assert b['latent_cooling_w']==0
    assert b['zone_rh_fraction']<.6
    c=solve_latent_load(**dict(args,control_enabled=False))
    assert c['latent_cooling_w']==0
    assert abs(c['moisture_balance_residual_kg_s'])<1e-14


@pytest.mark.parametrize('start',[744,194*24])
def test_nbuilding_on_off_native_equivalence(nbuilding,start):
    model,weather=nbuilding
    off=build_energy_input(model,weather,start_hour=start,hours=24)
    on=build_energy_input(model,weather,start_hour=start,hours=24,latent={'enabled':True})
    assert len(on.zones)==37
    assert not off.latent_enabled
    assert on.latent_enabled
    assert off.signature==on.signature  # moisture is not thermal checkpoint state
    assert off.hour(0)==on.hour(0)
    old_rows=[];new_rows=[]
    a=EnergyRunner(energy_input=off,on_hour=lambda h,rows:old_rows.append(rows)).run()
    b=EnergyRunner(energy_input=on,on_hour=lambda h,rows:new_rows.append(rows)).run()
    np.testing.assert_array_equal(a.zone_temperature_c,b.zone_temperature_c)
    np.testing.assert_array_equal(a.zone_hourly_energy_kwh,b.zone_hourly_energy_kwh)
    assert a.final_state==b.final_state
    assert b.max_moisture_residual_kg_s<1e-12
    assert b.latent_load['equipment_model'] is False
    for hour,(old,new) in enumerate(zip(old_rows,new_rows)):
        for i,(before,after) in enumerate(zip(old,new)):
            assert all(after[k]==v for k,v in before.items())
            assert after['total_cooling_w']==pytest.approx(after['sensible_cooling_w']+after['latent_cooling_w'])
            np.testing.assert_allclose(b.zone_latent_cooling_w[i,hour],after['latent_cooling_w'])
    assert a.latent_load=={'enabled':False}
    assert not b.zone_latent_cooling_w.flags.writeable


def test_nbuilding_rdf_roundtrip_sources_and_schedule(nbuilding,tmp_path):
    model,weather=nbuilding
    model.energy_settings['latent']={'enabled':True}
    space=model.spaceList[0]
    model.schedule['PERSON_MOISTURE']={'type':'Daily','value':[.04]*12+[.08]*12,'value_mode':'absolute'}
    space.settings['zone_popmoisture']='PERSON_MOISTURE'
    space.settings['zone_c_hum']=.55
    space.explicit_settings=list(set(space.explicit_settings)|{'zone_popmoisture','zone_c_hum'})
    with (tmp_path/'rdf.log').open('w') as f,contextlib.redirect_stdout(f):
        model.save(tmp_path/'roundtrip.ttl')
        restored=MoosasModel.load(tmp_path/'roundtrip.ttl')
    assert restored.energy_settings['latent']=={'enabled':True}
    other=next(s for s in restored.spaceList if s.id==space.id)
    assert other.settings['zone_popmoisture']=='PERSON_MOISTURE'
    assert other.settings['zone_c_hum']==.55
    r=build_energy_input(restored,weather,hours=24)
    z=next(z for z in r.zones if z.zone_id==str(space.id))
    assert np.all(z.cool_rh_fraction==.55)
    np.testing.assert_allclose(z.fresh_air_m3_h+z.infiltration_m3_h,z.ventilation_m3_h)
    assert np.all(z.people_moisture_kg_s>=0)
    model.energy_settings.pop('latent')


@pytest.mark.parametrize('flag',[1,'true',None])
def test_invalid_switch(nbuilding,flag):
    m,w=nbuilding
    with pytest.raises(ValueError):build_energy_input(m,w,hours=24,latent={'enabled':flag})


def test_geometry_repair_keeps_holes():
    from shapely.geometry import Polygon
    from MoosasPy.transform.geometry.geos import planar_surface
    p=Polygon([(0,0),(4,0),(4,4),(0,4)],holes=[[(1,1),(2,1),(2,2),(1,2)]])
    assert planar_surface(p).area==15
    assert len(planar_surface(p).interiors)==1


def test_uncontrolled_sealed_zone_has_no_steady_solution():
    r=solve_latent_load(zone_c=26,outdoor_c=30,pressure_pa=101325,outdoor_humidity_ratio=.018,
        people_moisture_kg_s=.001,fresh_air_m3_s=0,infiltration_m3_s=0,rh_max=.6,control_enabled=False)
    assert r['status']=='no_steady_state'
    assert r['zone_humidity_ratio_kg_kg'] is None
    assert r['dehumidification_kg_s']==0
    assert r['moisture_balance_residual_kg_s']==.001


def test_epw_cold_humidity_is_positive_and_pressure_dependent(tmp_path):
    from test.test_native_resources import NativeToolResourceTests
    from MoosasPy.simulation.weather import write_epw_csv
    epw=tmp_path/'cold.epw';NativeToolResourceTests._write_epw_fixture(epw)
    lines=epw.read_text().splitlines()
    for i in range(8,len(lines)):
        record=lines[i].split(',');record[7]='-40';record[9]='80000';lines[i]=','.join(record)
    epw.write_text('\n'.join(lines)+'\n')
    csv=tmp_path/'cold.csv';write_epw_csv(epw,csv)
    values=np.loadtxt(csv,delimiter=',')
    assert np.all(values[:,4]==.15)  # g/kg at -40 C dew point, 80 kPa
