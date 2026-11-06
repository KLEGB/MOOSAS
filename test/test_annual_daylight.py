from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import contextlib
import json
import numpy as np
import pytest
import shapely
from rdflib import URIRef, Literal

from MoosasPy.model import MoosasModel, apply_building_template
from MoosasPy.model.io.rdf import MoosasRDF, _decode_space_settings
from MoosasPy.simulation.radiation.runner import (
    AnnualDaylightRequest, RadianceCommandError, RadianceRunner, _daylight_metrics,
    _daylight_sensors, _read_daylight_wea, _read_radiance_matrix,
)
from MoosasPy.simulation.radiation.radiance import _visible_material, _get_sky
from MoosasPy.simulation.weather import convert_epw_to_wea
from MoosasPy.simulation.airflow.network import AfnPath


def test_area_weighted_sda_udi_and_night_occupancy():
    total = np.zeros((2,8760),np.float32)
    direct = np.zeros_like(total)
    total[0,:4] = [0,100,300,2000]
    total[1,:4] = [0,99,299,2001]
    direct[0,1] = 1001
    sensors = [dict(sensor_id='a',space_id='room',represented_area_m2=1),
               dict(sensor_id='b',space_id='room',represented_area_m2=3)]
    from MoosasPy.model.building_templates import building_templates
    records = building_templates()
    space = SimpleNamespace(id='room', settings={'zone_template': next(iter(records))}, explicit_settings=[])
    model = SimpleNamespace(buildingTemplate=records, schedule={})
    mask = [True]*4+[False]*8756
    request = AnnualDaylightRequest('unused',occupied_by_space={'room':mask},ase_hours=0)
    points, spaces, _ = _daylight_metrics(total,direct,sensors,[space],request,model)
    assert points[0]['da']==.5
    assert points[0]['udi_useful']==.75
    assert spaces[0]['sda']==.25
    assert spaces[0]['ase']==.25
    assert all(np.isclose(p['udi_low']+p['udi_useful']+p['udi_high'],1) for p in points)
    empty = AnnualDaylightRequest('unused',occupied_by_space={'room':[False]*8760})
    assert _daylight_metrics(total,direct,sensors,[space],empty,model)[1][0]['sda'] is None
    from MoosasPy.model.building_templates import template_parameter
    _, _, masks = _daylight_metrics(total,direct,sensors,[space],AnnualDaylightRequest('unused'),model)
    np.testing.assert_array_equal(masks['room'],
        np.asarray(template_parameter(records[space.settings['zone_template']], 'zone_ppsm')) > 0)
    space.settings['zone_ppsm'] = 0.
    space.explicit_settings.append('zone_ppsm')
    assert not _daylight_metrics(total,direct,sensors,[space],AnnualDaylightRequest('unused'),model)[2]['room'].any()


def test_grid_preserves_holes_all_pieces_and_area():
    first = shapely.Polygon([(0,0,0),(3,0,0),(3,3,0),(0,3,0)],holes=[[(1,1,0),(2,1,0),(2,2,0),(1,2,0)]])
    second = shapely.Polygon([(5,0,0),(6,0,0),(6,1,0),(5,1,0)])
    floors=[SimpleNamespace(Uid='floor',face=shapely.MultiPolygon([first,second]))]
    space=SimpleNamespace(id='room',getAllFaces=lambda to_dict:{'MoosasFloor':floors})
    sensors,_=_daylight_sensors(SimpleNamespace(spaceList=[space]),AnnualDaylightRequest('x',grid_spacing_m=.7))
    assert np.isclose(sum(s['represented_area_m2'] for s in sensors),9)
    region=shapely.force_2d(floors[0].face)
    assert all(region.covers(shapely.Point(s['x'],s['y'])) for s in sensors)
    assert all(s['z']==.76 for s in sensors)


def test_vlt_is_not_shgc_and_pit_uses_west_positive_and_fractional_time():
    a,record=_visible_material({'visible_transmittance':.6,'shgc':.1},'window',True,'window')
    b,_=_visible_material({'visible_transmittance':.6,'shgc':.9},'window',True,'window')
    assert a==b and record['visible_transmittance']==.6
    assert '3 0.6 0.6 0.6' not in a
    sky=_get_sky(datetime(2001,6,21,12,30),'-c',28,77,utc_offset_hours=5.5)
    assert '12.5' in sky and '-o -77.0' in sky and '-m -82.5' in sky
    with pytest.raises(ValueError,match='clear glass'):
        _visible_material({'visible_transmittance':1},'window',True,'window')


def test_epw_fractional_timezone_and_midpoints(tmp_path):
    source=Path(__file__).parents[1]/'MoosasPy/db/CHN_BJ_Beijing-Nanyuan.AP.545120_TMYx.epw'
    rows=source.read_text().splitlines()
    header=rows[0].split(',');header[8]='5.5';header[7]='116.3834567';rows[0]=','.join(header)
    epw=tmp_path/'test.epw';epw.write_text('\n'.join(rows)+'\n')
    wea=convert_epw_to_wea(epw,tmp_path/'test.wea')
    _,_,location=_read_daylight_wea(wea)
    assert location['utc_offset_hours']==5.5
    assert float(Path(wea).read_text().splitlines()[2].split()[1]) == -116.3834567
    assert len(Path(wea).read_text().splitlines()) == 8766
    assert Path(wea).read_text().splitlines()[6].split()[:3]==['1','1','0.5']
    bad=rows[8].split(',');bad[14]='9999';rows[8]=','.join(bad)
    epw.write_text('\n'.join(rows)+'\n')
    with pytest.raises(ValueError,match='DNI'):
        convert_epw_to_wea(epw,tmp_path/'missing.wea')
    epw.write_text('\n'.join(rows[:-1])+'\n')
    with pytest.raises(ValueError,match='8760'):
        convert_epw_to_wea(epw,tmp_path/'short.wea')

def test_matrix_rejects_truncated_or_wrong_dimension(tmp_path):
    file=tmp_path/'test.mtx'
    file.write_text('#?RADIANCE\nNROWS=1\nNCOLS=2\nNCOMP=3\nFORMAT=ascii\n\n1 2 3\n')
    with pytest.raises(ValueError,match='incomplete'):
        _read_radiance_matrix(file,1,2)
    with pytest.raises(ValueError,match='dimensions'):
        _read_radiance_matrix(file,2,1)


@pytest.mark.parametrize('missing', ['NROWS', 'NCOLS', 'NCOMP'])
def test_matrix_requires_declared_dimensions(tmp_path, missing):
    file = tmp_path / 'missing-header.mtx'
    header = dict(NROWS=1, NCOLS=1, NCOMP=3, FORMAT='ascii')
    del header[missing]
    file.write_text('#?RADIANCE\n' + ''.join(f'{key}={value}\n' for key, value in header.items()) + '\n1 2 3\n')
    with pytest.raises(ValueError, match='dimensions'):
        _read_radiance_matrix(file, 1, 1)


@pytest.mark.parametrize('format,dtype', [('ascii', None), ('float', np.float32), ('double', np.float64)])
def test_matrix_reads_declared_rgb_formats(tmp_path, format, dtype):
    file = tmp_path / 'valid.mtx'
    header = f'#?RADIANCE\nNROWS=1\nNCOLS=2\nNCOMP=3\nFORMAT={format}\n\n'.encode('ascii')
    values = np.arange(6, dtype=np.float32)
    payload = b'0 1 2 3 4 5\n' if dtype is None else values.astype(dtype).tobytes()
    file.write_bytes(header + payload)
    np.testing.assert_array_equal(_read_radiance_matrix(file, 1, 2), values.reshape(1, 2, 3))


def test_explicit_tool_directory_never_falls_back(tmp_path, monkeypatch):
    import shutil
    import os
    alternate = tmp_path / ('gendaymtx.exe' if os.name == 'nt' else 'gendaymtx')
    alternate.write_bytes(b'not a real executable')
    monkeypatch.setattr(shutil, 'which', lambda name: str(alternate))
    runner = RadianceRunner(SimpleNamespace(), executable_dir=tmp_path / 'missing')
    with pytest.raises(RadianceCommandError, match='not found'):
        runner._executable('gendaymtx')


def test_unbundled_platform_requires_explicit_tools(tmp_path, monkeypatch):
    import platform
    monkeypatch.setattr(platform, 'machine', lambda: 'arm64')
    with pytest.raises(OSError, match='executable override'):
        RadianceRunner(SimpleNamespace())
    assert RadianceRunner(SimpleNamespace(), executable_dir=tmp_path).executable_dir == tmp_path


def test_ttl_rejects_misspelled_infiltration_key():
    graph = MoosasRDF()
    subject = URIRef('urn:room')
    graph.add((subject, graph.moosas.hasSetting, Literal('zone_inflitration')))
    graph.add((subject, graph.moosas.zone_inflitration, Literal(.5)))
    with pytest.raises(ValueError, match='use zone_infiltration'):
        _decode_space_settings(graph, subject, SimpleNamespace(settings={}))


@pytest.mark.slow
def test_ttl_canonical_settings_reach_airflow_without_duplicate_snapshot(tmp_path):
    source=Path(__file__).parent/'caseFile/NBuilding.rdf'
    with contextlib.redirect_stdout((tmp_path/'load.log').open('w')):
        model=MoosasModel.load(source)
        apply_building_template(model, 'CN/GB55015-2021/3A/Office')
        window=model.glazingList[0]
        window.settings.update(visible_transmittance=.61,solar_transmittance=.42,operable=.37,
                               airflow_element={'cd':.61}, numeric_string='001')
        target=tmp_path/'roundtrip.ttl';model.save(target)
        graph=MoosasRDF.load(target)
        metadata_uri = URIRef('urn:moosas:energy-model')
        metadata_key = URIRef('urn:moosas:energy-metadata')
        metadata = json.loads(str(next(graph.objects(metadata_uri, metadata_key))))
        for records in (metadata['spaces'], metadata['elements']):
            assert all('settings' not in record and 'explicit_settings' not in record for record in records.values())
        metadata['elements'][str(window.Uid)]['settings'] = {'visible_transmittance': .99}
        graph.set((metadata_uri, metadata_key, Literal(json.dumps(metadata))))
        subject=next(graph.subjects(graph.moosas.Uid,Literal(str(window.Uid))))
        graph.set((subject,graph.moosas.visible_transmittance,Literal(.51)))
        graph.serialize(target,format='turtle')
        loaded=MoosasModel.load(target)
    result=next(w for w in loaded.glazingList if w.Uid==window.Uid)
    assert result.settings['visible_transmittance']==.51
    assert result.settings['solar_transmittance']==.42
    assert result.settings['airflow_element']=={'cd':.61}
    assert result.settings['numeric_string']=='001'
    path=AfnPath.fromElement(result)
    assert path.element['cd']==.61 and path.element['operable']==.37
    material,record=_visible_material(result.settings,'window',True,'window')
    assert record['visible_transmittance']==.51


@pytest.mark.slow
def test_real_radiance_unobstructed_sensor(tmp_path):
    """Native transport sanity: horizontal plane receives sky and exact sun."""
    # A tiny notional room supplies only the analysis floor; the optical scene
    # is empty, so the sensor sees the unobstructed sky and sun.
    floor=SimpleNamespace(Uid='f',face=shapely.Polygon([(0,0,0),(1,0,0),(1,1,0),(0,1,0)]),category=2)
    space=SimpleNamespace(id='room',settings={},explicit_settings=[],getAllFaces=lambda to_dict:{'MoosasFloor':[floor],'MoosasCeiling':[]})
    model=SimpleNamespace(spaceList=[space],wallList=[],glazingList=[],skylightList=[],shadingList=[],buildingTemplate={},schedule={})
    apply_building_template(model, 'CN/GB55015-2021/3A/Office')
    epw=Path(__file__).parents[1]/'MoosasPy/db/CHN_BJ_Beijing-Nanyuan.AP.545120_TMYx.epw'
    wea=convert_epw_to_wea(epw,tmp_path/'test.wea')
    result=RadianceRunner(model,timeout_seconds=120).run_annual(AnnualDaylightRequest(wea,output_dir=str(tmp_path),grid_spacing_m=1,sky_subdivision=1,ambient_divisions=1024))
    total=np.load(result.illuminance_total_file)
    direct=np.load(result.illuminance_direct_file)
    assert total.shape==(1,8760) and result.workspace.retained
    assert total.max()>10000 and direct.max()>10000
    assert np.max(np.abs(total[:,np.arange(8760)%24==0]))==0
    assert np.quantile((total-direct).ravel(),.1)>=-1
    assert all(command.returncode==0 for command in result.commands)
    # Direct noon cross-check against native rtrace using just this sun source.
    root=Path(result.workspace.path)
    hour=int(np.argmax(direct[0]))
    modifiers=(root/'sunmods.txt').read_text().split()
    name=f'solar{hour*60+30}'
    suns=(root/'suns.rad').read_text()
    source=suns[suns.index('\nvoid light '+name+'\n'):]
    end=source.find('\nvoid light ',1)
    if end>=0:source=source[:end]
    (root/'one-sun.rad').write_text(source)
    runner=RadianceRunner(model)
    with (root/'one-sun.oct').open('wb') as out:
        runner._run_command((str(runner._executable('oconv')),'one-sun.rad'),root,stdout=out)
    with (root/'sensors.pts').open() as inp:
        traced=runner._run_command((str(runner._executable('rtrace')),'-I+','-h','-ab','0','-dt','0','one-sun.oct'),root,stdin=inp)
    expected=runner._parse_illuminances(traced.stdout)[0]
    assert np.isclose(direct[0,hour],expected,rtol=.01)


def test_native_weather_pit_prepares_perez_sky(tmp_path):
    from MoosasPy.simulation.radiation import RadianceSky
    from MoosasPy.simulation.contracts import Location
    with contextlib.redirect_stdout((tmp_path/'load.log').open('w')):
        model=MoosasModel.load(Path(__file__).parent/'caseFile/NBuilding.rdf')
    model.spaceList=model.spaceList[:1]
    apply_building_template(model, 'CN/GB55015-2021/3A/Office')
    sky=RadianceSky(datetime(2001,6,21,12,30),'-c',
        Location('x','Beijing','BJ',39.783,116.383,40,101325),utc_offset_hours=8,
        direct_normal_irradiance_w_m2=600,diffuse_horizontal_irradiance_w_m2=120)
    runner=RadianceRunner(model,sky)
    floors=runner._write_inputs(tmp_path/'model.rad',tmp_path/'points.pts')
    assert floors and len(runner._input_commands)==1
    assert 'gendaylit' in runner._input_commands[0].command[0]
    assert 'skyfunc' in (tmp_path/'model.rad').read_text()
    assert runner._summarize_floors(floors,(100.,)*sum(count for _,count in floors))[0].daylight_factor is None
    # The ideal-sky branch must still compile after shared-scene refactoring.
    from MoosasPy.simulation.radiation.scene import _model_to_radiance
    _model_to_radiance(model,sky.date,'-c',39.783,116.383,rad_path=str(tmp_path/'ideal.rad'),utc_offset_hours=8)
    command=runner._compile_scene(tmp_path/'ideal.rad',tmp_path/'ideal.oct',tmp_path)
    assert command.returncode==0 and (tmp_path/'ideal.oct').stat().st_size>0


def test_paired_channels_match_analytic_diffuse_reflection(tmp_path):
    """Uniform unit sky/ground plus a 50%-reflecting wall has E~.75*pi.

    Half of the horizontal sensor hemisphere sees unit sky directly. The
    other half sees wall radiance .5. This checks reflected light independently
    of the annual metric implementation and also checks direct~.5*pi.
    """
    from MoosasPy.simulation.radiation.scene import _daylight_scene
    wall=SimpleNamespace(Uid='wall',category=0,settings={'visible_reflectance':.5},
        face=shapely.Polygon([(0,-100,-100),(0,100,-100),(0,100,100),(0,-100,100)]),
        normal=np.array([1,0,0]),glazingElement=[])
    model=SimpleNamespace(spaceList=[],wallList=[wall],glazingList=[],skylightList=[],shadingList=[])
    (tmp_path/'scene.rad').write_text(_daylight_scene(model,split_transport=True)[0])
    (tmp_path/'receiver.rad').write_text('#@rfluxmtx h=u\nvoid glow ground_g\n0\n0\n4 1 1 1 0\nground_g source ground\n0\n0\n4 0 0 -1 180\n#@rfluxmtx h=r1 u=Y\nvoid glow sky_g\n0\n0\n4 1 1 1 0\nsky_g source sky\n0\n0\n4 0 0 1 180\n')
    (tmp_path/'point.pts').write_text('.5 0 .76 0 0 1\n')
    runner=RadianceRunner(model)
    with (tmp_path/'point.pts').open() as inp,(tmp_path/'coeff.mtx').open('wb') as out:
        runner._run_command((str(runner._executable('rfluxmtx')),'-I+','-y','1','-ad','16384','-ab','2','-aa','0','-lw','.000001','-faf','-','receiver.rad','scene.rad'),tmp_path,stdin=inp,stdout=out)
    coeff=_read_radiance_matrix(tmp_path/'coeff.mtx',1,146)
    assert np.all(coeff[:,:,0]>=coeff[:,:,1]-1e-6)
    assert np.isclose(coeff[0,:,0].sum(),.75*np.pi,rtol=.02)
    assert np.isclose(coeff[0,:,1].sum(),.5*np.pi,rtol=.02)
