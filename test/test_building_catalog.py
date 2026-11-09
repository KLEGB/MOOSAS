"""V5 import, replacement and solver-input semantics (no native executables)."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from MoosasPy.model import apply_building_template, building_templates
from MoosasPy.model.building_catalog import catalog_controls
from MoosasPy.model.thermal_templates import resolve_surface_template, template_program
from MoosasPy.simulation.energy.input import EnergyRequest, SurfaceRequest, ZoneRequest
from MoosasPy.simulation.radiation.scene import _generate_radiance_geometry


def constant(value, unit='1'):
    return dict(mode='constant', value=value, unit=unit)


@pytest.fixture
def catalog(tmp_path):
    (tmp_path / 'schedules').mkdir()
    schedules = {
        'people': dict(unit='1', values=[.25] * 8760),
        'heat': dict(unit='degC', values=[5.] * 8760),
        'cool': dict(unit='degC', values=[None] + [26.] * 8759),
    }
    (tmp_path / 'schedules/office.sch').write_text(json.dumps(dict(
        schema='moosas.schedule', version=2, schedules=schedules)))
    def schedule(name, unit='1'):
        return dict(mode='schedule', file='schedules/office.sch', name=name, unit=unit)
    template = dict(metadata=dict(schema='moosas.building-template', id='Office_2B',
        building_type='Office', standard_name='GB55015-2021'),
        Space=dict(default=dict(internal_gains=dict(occupancy_density=constant(.1, 'person/m2'),
            occupancy_fraction=schedule('people'), lighting_power_density=constant(8, 'W/m2'),
            equipment_power_density=constant(15, 'W/m2')),
            air_exchange=dict(fresh_air=constant(30, 'm3/h/person'),
                infiltration=constant(.5, '1/h'), fresh_air_availability=constant(0)),
            system=dict(heating_setpoint=schedule('heat', 'degC'),
                cooling_setpoint=schedule('cool', 'degC'), hvac_availability=constant(0),
                heating_availability=constant(1), cooling_availability=constant(1)))),
        Envelope=dict(exterior_wall=dict(u=constant(.45, 'W/(m2 K)'), visible_reflectance=constant(.6)),
            window=dict(visible_transmittance=constant(.7), opening_ratio=constant(0),
                u=constant(1.3, 'W/(m2 K)'), shgc=constant(.25))))
    source = tmp_path / 'Office_2B.json'
    source.write_text(json.dumps(dict(BuildingTemplate=template)))
    (tmp_path / 'library.json').write_text(json.dumps(dict(
        schema='moosas.building-template-library', version=5, template_assumptions={})))
    return source


def test_annual_fractions_null_and_independent_controls(catalog):
    record = building_templates(catalog)['Office_2B']
    settings = dict(record)
    controls = catalog_controls(settings)
    assert controls['heat'][0] == 5
    assert controls['cool_enabled'][0] == 0
    assert controls['cool_enabled'][1] == 1
    assert controls['enabled'][0] == 1  # general HVAC availability=0 must not erase setback heating
    np.testing.assert_allclose(template_program(settings, 'zone_ppsm', .1), .025)
    np.testing.assert_array_equal(template_program(settings, 'zone_pfav', 30), 0)
    surface, _ = resolve_surface_template(settings, 'window', 'outdoors', [0, 1, 0])
    assert surface['visible_transmittance'] == .7
    assert surface['solar_transmittance'] == pytest.approx(.15)
    assert surface['solar_reflectance'] == .75


def test_invalid_schedule_is_rejected(catalog):
    path = catalog.parent / 'schedules/office.sch'
    data = json.loads(path.read_text())
    data['schedules']['people']['values'][0] = None
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='null is only allowed'):
        building_templates(catalog)


def test_full_replacement_discards_stale_parameters_preserves_geometry(catalog):
    opening = SimpleNamespace(Uid='window', settings=dict(operable=.9, layers=[1]),
        explicit_settings={'layers'}, normal=(0, 1, 0), operable=.9)
    wall = SimpleNamespace(Uid='wall', settings=dict(u_value=9., layers=[1],
        boundary_condition='outdoors', side_overrides={'room': {'u_value': 8., 'boundary_condition': 'outdoors'}}),
        explicit_settings={'u_value'}, normal=(0, 1, 0), isOuter=True, glazingElement=[opening],
        coordinates=[(0, 0, 0), (1, 0, 0), (1, 0, 1)])
    def faces(to_dict=False):
        return {'MoosasWall': [wall]} if to_dict else [wall, opening]
    room = SimpleNamespace(id='room', settings=dict(zone_name='Room', zone_h_temp=99.,
        initial_temperature_c=80, floor_area_m2=99, _thermal_template={'id': 'old'}),
        explicit_settings=['zone_h_temp'], conditioned=False, level=0, getAllFaces=faces)
    model = SimpleNamespace(spaceList=[room], voidList=[], wallList=[wall], glazingList=[opening],
        buildingTemplate={'old': {}}, energy_settings=dict(zone_overrides={'room': {'zone_h_temp': 99}}),
        schedule={'old': {}}, scheduleByType={'old': {}}, schedulePath='old')
    geometry = list(wall.coordinates)
    apply_building_template(model, template_path=catalog, replace_all=True)
    assert room.settings['zone_h_temp'] == 5
    assert 'initial_temperature_c' not in room.settings
    assert 'floor_area_m2' not in room.settings
    assert room.conditioned
    assert 'layers' not in wall.settings
    assert wall.settings['u_value'] == .45
    assert wall.settings['side_overrides'] == {'room': {'boundary_condition': 'outdoors'}}
    assert opening.operable == 0
    assert not model.schedule
    assert 'zone_overrides' not in model.energy_settings
    assert wall.coordinates == geometry
    assert wall.isOuter


def test_native_input_disables_only_the_requested_mode():
    values = np.ones(8760)
    surface = SurfaceRequest(surface_id='wall', source_element_id='wall', zone_id='room',
        role='wall', boundary_condition='adiabatic', area_m2=10, resistance_m2k_w=1,
        thermal_mass_j_m2k=60000, reflectance=.3, transmittance=0, emissivity=.9,
        inside_emissivity=.9, hi_w_m2k=3, ho_w_m2k=0, sky_view=0, ground_view=0,
        ground_contact_w_m2k=0)
    zone = ZoneRequest('room', 'Room', 10, 30, (surface,), 3.5, 2.6, 20, .6, .6,
        values*100, values*10, values*10, values*5, values*0, values*10, values,
        heating_enabled=values, cooling_enabled=values*0)
    weather = SimpleNamespace(temperature=values*10, ground_temperature=values*10,
        sky_temperature=values*280, pressure=values*101325, wind_speed=values*2)
    request = EnergyRequest((zone,), weather, (np.zeros((1, 8760)),))
    row = request.hour(0)[0]
    assert row['hvac_enabled'] and row['heating_enabled']
    assert not row['cooling_enabled']
    assert row['heat_setpoint_c'] == 5
    assert row['cool_setpoint_c'] == 1e6


def test_radiance_uses_visible_optics_instead_of_shgc():
    glazing = SimpleNamespace(Uid='glass', category=1,
        settings=dict(visible_transmittance=.7, shgc=.25), representation=lambda: object())
    with patch('MoosasPy.simulation.radiation.scene._mesh_to_radiance_object', return_value='polygon\n'):
        scene = _generate_radiance_geometry([], [], [glazing])
    transmissivity = (np.sqrt(.8402528435+.0072522239*.7*.7)-.9166530661)/(.0036261119*.7)
    assert f'3 {transmissivity}' in scene


def test_shared_overlap_is_opt_in_and_counted_once_as_adiabatic():
    import shapely
    from MoosasPy.simulation.energy.input import _adjacency_parts
    from MoosasPy.transform.geometry.geos import Projection, Vector
    spaces={name:SimpleNamespace(id=name,force_2d=lambda:shapely.box(0,0,2,2)) for name in ('a','b','c')}
    projection=Projection(Vector([0,0,0]),Vector([0,0,1]))
    footprint=shapely.force_2d(projection.toUV(shapely.Polygon([(0,0,0),(2,0,0),(2,2,0),(0,2,0)])))
    args=(spaces['a'],set(spaces),spaces,footprint,projection,np.array([0.,0.,1.]),np.array([0.,0.,0.]))
    with pytest.raises(ValueError,match='overlapping boundary owners'):
        _adjacency_parts(*args)
    diagnostics=[]
    parts=_adjacency_parts(*args,allow_adiabatic_overlap=True,diagnostics=diagnostics)
    assert sum(part.area for _,part,_ in parts)==pytest.approx(4.)
    assert all(boundary=='adiabatic' for boundary,_,_ in parts)
    assert len(diagnostics)==1 and '4 m2' in diagnostics[0]


def test_explicit_wall_normal_uses_locally_supported_wall_portion():
    import shapely
    from MoosasPy.transform.geometry.element import MoosasEdge
    edge=object.__new__(MoosasEdge)
    edge._MoosasEdge__explicitBoundary=True
    edge._MoosasEdge__botBound=shapely.Polygon([(44.87,11.34),(44.87,16.07),(43.27,16.07),(43.86,11.34)])
    face=shapely.Polygon([(43.27,16.07,26.15),(43.86213044194087,11.340266716842674,26.15),
        (43.86213044194087,11.340266716842674,27),(43.27,16.07,27)])
    edge.wall=[SimpleNamespace(face=face)]
    before=face.wkb
    normal=edge.FactorOfWall[0].array
    assert normal[0]<0 and normal[1]<0
    assert face.wkb==before


def test_legacy_tilted_wall_uses_unique_parallel_explicit_ring_without_snapping():
    import shapely
    from MoosasPy.transform.geometry.element import MoosasEdge
    edge=object.__new__(MoosasEdge)
    edge._MoosasEdge__explicitBoundary=True
    edge._MoosasEdge__botBound=shapely.Polygon([(64.26,18.88),(64.26,33.52),(57.14,33.52),(55.89,18.88)])
    face=shapely.Polygon([(56.05,20.79,45),(55.88685162149304,18.880268966068854,45.00000065848706),
        (55.887785982498116,18.88018914358059,49.50000046306436),(57.138476574809715,33.520130146259014,49.50000031862617),
        (57.13754221380464,33.52020996874728,45.00000051404887)])
    edge.wall=[SimpleNamespace(Uid='legacy_panel',face=face)]
    before=face.wkb
    with pytest.warns(RuntimeWarning,match='geometry unchanged'):
        normal=edge.FactorOfWall[0].array
    assert normal[0]<0 and normal[1]>0
    assert face.wkb==before
