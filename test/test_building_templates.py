from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from MoosasPy.model import MoosasModel
from MoosasPy.model.metadata import apply_energy_metadata, encode_energy_metadata
from MoosasPy.model.building_templates import (
    DEFAULT_LIBRARY, apply_building_template, building_templates, load_building_template_library,
    resolve_surface_template, template_program,
)


OFFICE_LARGE_3A = 'CN/GB55015-2021/3A/OfficeLarge'


def template_settings(record):
    return {'_building_template': deepcopy(record), 'zone_template': record['id']}


@pytest.fixture(scope='module')
def office_large_3a_record():
    load_building_template_library()
    return building_templates(DEFAULT_LIBRARY)[OFFICE_LARGE_3A]


def _strip_resolved_schedules(value):
    if isinstance(value, dict):
        if value.get('mode') == 'schedule':
            return {key: item for key, item in value.items() if key != 'values'}
        return {key: _strip_resolved_schedules(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_strip_resolved_schedules(item) for item in value]
    return value


@pytest.fixture
def international_library(tmp_path, office_large_3a_record):
    record = office_large_3a_record
    standard = record['standard']
    namespace = f"{standard['country']}_{standard['id']}"
    package = tmp_path / namespace
    package.mkdir()
    schedule_sources = {}
    for domain in record['BuildingTemplate']['Space'].values():
        for value in domain.values():
            if value.get('mode') == 'schedule':
                schedule_sources.setdefault(value['file'], {})[value['name']] = {
                    'unit': value['unit'],
                    'values': value['values'],
                }
    for filename, schedules in schedule_sources.items():
        schedule_file = package / filename
        schedule_file.parent.mkdir(parents=True, exist_ok=True)
        schedule_file.write_text(json.dumps({
            'schema': 'moosas.schedule',
            'version': 1,
            'schedules': schedules,
        }), encoding='utf-8')
    manifest = {
        'schema': 'moosas.building-template-library',
        'version': 4,
        'standards': [namespace],
        'standard_metadata': standard,
        'template_assumptions': record['metadata']['energy_assumptions'],
    }
    (tmp_path / 'library.json').write_text(json.dumps(manifest), encoding='utf-8')

    template = {
        'BuildingTemplate': {
            'metadata': record['metadata']['template_metadata'],
            **_strip_resolved_schedules(record['BuildingTemplate']),
        }
    }
    metadata = record['metadata']['template_metadata']
    filename = f"{metadata['building_type']}_{metadata['climate_zone']}.json"
    (package / filename).write_text(json.dumps(template), encoding='utf-8')
    return tmp_path


def test_bundled_default_catalog_registers_office_large_3a():
    library = load_building_template_library()
    assert set(library['standards']) == {'CN/GB55015-2021'}
    records = building_templates()
    assert len(records) == 1
    assert DEFAULT_LIBRARY == Path(__file__).parents[1] / 'MoosasPy' / 'db' / 'template'
    assert (DEFAULT_LIBRARY / 'CN_GB55015-2021' / 'schedules' / 'office.sch').is_file()
    expected_climates = {'3A'}
    expected_buildings = {'OfficeLarge'}
    assert {record['metadata']['template_metadata']['climate_zone'] for record in records.values()} == expected_climates
    assert {record['metadata']['template_metadata']['building_type'] for record in records.values()} == expected_buildings
    assert all(record_id.startswith('CN/GB55015-2021/') for record_id in records)

    record = records['CN/GB55015-2021/3A/OfficeLarge']
    assert set(record) == {'schema', 'version', 'id', 'standard', 'BuildingTemplate', 'metadata'}
    assert set(record['BuildingTemplate']['Envelope']) == {
        'exterior_wall', 'interior_wall', 'floor', 'ground', 'roof', 'window', 'skylight'}
    settings = template_settings(record)
    wall, _ = resolve_surface_template(settings, 'wall', 'outdoors')
    window, _ = resolve_surface_template(settings, 'window', 'outdoors')
    ground, _ = resolve_surface_template(settings, 'floor', 'ground')
    assert wall['u_value'] == .6
    assert window['u_value'] == 1.8
    assert window['solar_transmittance'] == .1
    assert window['solar_reflectance'] == .8
    assert ground['resistance_m2k_w'] == 1.2
    assert len(record['BuildingTemplate']['Space']['system']['hvac_availability']['values']) == 8760


def test_direct_catalog_schedules_and_snapshots_are_self_contained(
        international_library, office_large_3a_record):
    record = building_templates(international_library)[OFFICE_LARGE_3A]
    assert record['schema'] == 'moosas.building-template'
    assert record['version'] == 4
    assert set(record['BuildingTemplate']) == {'Space', 'Envelope'}
    values = template_program(template_settings(record), 'zone_ppsm', 2.)
    assert values.shape == (8760,)
    expected_values = office_large_3a_record['BuildingTemplate']['Space']['internal_gains']['occupancy_density']['values']
    np.testing.assert_array_equal(values[:48], expected_values[:48])

    space = SimpleNamespace(id='z1', settings={'zone_template': None},
                            explicit_settings=[], conditioned=True, getAllFaces=lambda **_: [])
    model = MoosasModel()
    model.spaceList = [space]
    model.metadata = {'project': {'name': 'schedule snapshot'}}
    apply_building_template(model, OFFICE_LARGE_3A, template_path=international_library)
    payload = encode_energy_metadata(model)
    (international_library / 'library.json').unlink()
    restored = MoosasModel()
    restored.spaceList = [deepcopy(space)]
    restored.spaceList[0].settings = {}
    apply_energy_metadata(restored, payload)
    assert restored.spaceList[0].settings == space.settings
    assert restored.metadata == model.metadata
    assert template_program(restored.spaceList[0].settings, 'zone_ppsm', 1.)[96] == expected_values[96]


@pytest.mark.parametrize('change, message', [
    ('metadata', 'metadata does not match'),
    ('envelope', 'invalid Envelope construction groups'),
    ('schedule_length', 'exactly 8760'),
    ('schedule_unit', 'unit does not match'),
    ('schedule_path', 'relative .sch'),
])
def test_invalid_direct_templates_fail_explicitly(international_library, change, message):
    folder = international_library / 'CN_GB55015-2021'
    template_file = folder / 'OfficeLarge_3A.json'
    template = json.loads(template_file.read_text(encoding='utf-8'))
    schedule_file = folder / 'schedules' / 'office.sch'
    schedule = json.loads(schedule_file.read_text(encoding='utf-8'))
    if change == 'metadata':
        template['BuildingTemplate']['metadata']['country'] = 'US'
    elif change == 'envelope':
        del template['BuildingTemplate']['Envelope']['ground']
    elif change == 'schedule_length':
        schedule['schedules']['occupancy_density']['values'].pop()
        schedule_file.write_text(json.dumps(schedule), encoding='utf-8')
    elif change == 'schedule_unit':
        schedule['schedules']['occupancy_density']['unit'] = '1'
        schedule_file.write_text(json.dumps(schedule), encoding='utf-8')
    elif change == 'schedule_path':
        template['BuildingTemplate']['Space']['internal_gains']['occupancy_density']['file'] = '..\\outside.sch'
    template_file.write_text(json.dumps(template), encoding='utf-8')
    with pytest.raises(ValueError, match=message):
        building_templates(international_library)


def test_mismatched_snapshot_is_rejected(international_library):
    record = building_templates(international_library)[OFFICE_LARGE_3A]
    settings = {'_building_template': record, 'zone_template': 'CN/GB55015-2021/3A/OfficeSmall'}
    with pytest.raises(ValueError, match='does not match'):
        template_program(settings, 'zone_ppsm', 1.)


def test_surface_values_are_direct_inputs_not_prescriptive_limits(international_library):
    record = building_templates(international_library)[OFFICE_LARGE_3A]
    settings = template_settings(record)
    window, _ = resolve_surface_template(settings, 'window', 'outdoors')
    window_source = record['BuildingTemplate']['Envelope']['window']
    shgc = window_source['shgc']['value']
    absorptance = min(record['metadata']['energy_assumptions']['glass_absorptance'], shgc)
    assert window['u_value'] == window_source['u']['value']
    assert window['solar_transmittance'] == pytest.approx(shgc - absorptance)
    assert window['solar_reflectance'] == pytest.approx(1 - shgc)
    assert '_template_limits' not in window
    assert '_template_context' not in window


def test_scheduled_surface_values_are_rejected(international_library):
    template_file = international_library / 'CN_GB55015-2021' / 'OfficeLarge_3A.json'
    template = json.loads(template_file.read_text(encoding='utf-8'))
    schedule_file = international_library / 'CN_GB55015-2021' / 'schedules' / 'office.sch'
    schedule = json.loads(schedule_file.read_text(encoding='utf-8'))
    schedule['schedules']['u_value'] = {
        'unit': 'W/(m2 K)', 'values': [1.] * 8760}
    schedule_file.write_text(json.dumps(schedule), encoding='utf-8')
    template['BuildingTemplate']['Envelope']['window']['u'] = {
        'mode': 'schedule', 'file': 'schedules/office.sch',
        'name': 'u_value', 'unit': 'W/(m2 K)'}
    template_file.write_text(json.dumps(template), encoding='utf-8')
    record = building_templates(international_library)[OFFICE_LARGE_3A]
    with pytest.raises(ValueError, match='does not support scheduled surface values'):
        resolve_surface_template(template_settings(record), 'window', 'outdoors')
