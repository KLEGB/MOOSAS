from copy import deepcopy
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import numpy as np
import pytest

from MoosasPy.model import MoosasModel
from MoosasPy.model.metadata import apply_energy_metadata, encode_energy_metadata
from MoosasPy.model.building_templates import (
    DEFAULT_LIBRARY, apply_building_template, building_templates, load_building_template_library,
    resolve_surface_template, space_template, resolve_zone_settings,
)


OFFICE_3A = 'CN/GB55015-2021/3A/Office'


@pytest.fixture(scope='module')
def templates():
    return building_templates()


@pytest.fixture(scope='module')
def office_record(templates):
    return templates[OFFICE_3A]


@pytest.fixture
def local_library(tmp_path):
    source = DEFAULT_LIBRARY
    package = source / 'CN' / 'GB55015-2021'
    target_package = tmp_path / 'CN' / 'GB55015-2021'
    (target_package / 'schedules').mkdir(parents=True)
    shutil.copyfile(source / 'library.json', tmp_path / 'library.json')
    shutil.copyfile(package / 'Office_3A.json', target_package / 'Office_3A.json')
    shutil.copyfile(package / 'schedules' / 'office.sch',
                    target_package / 'schedules' / 'office.sch')
    return tmp_path


def _space(space_id='z1', settings=None):
    return SimpleNamespace(
        id=space_id, settings={} if settings is None else settings,
        explicit_settings=[], conditioned=True, getAllFaces=lambda **_: [],
    )


def test_bundled_catalog_contains_only_office_3a(templates):
    library = load_building_template_library()
    assert 'version' not in library
    assert set(library['standards']) == {'CN/GB55015-2021'}
    assert set(templates) == {OFFICE_3A}
    assert DEFAULT_LIBRARY == Path(__file__).parents[1] / 'MoosasPy' / 'db' / 'template'
    assert [path.name for path in (DEFAULT_LIBRARY / 'CN' / 'GB55015-2021').glob('*.json')] == [
        'Office_3A.json']
    assert [path.name for path in (
        DEFAULT_LIBRARY / 'CN' / 'GB55015-2021' / 'schedules').glob('*.sch')] == ['office.sch']


def test_explicit_external_catalog_can_contain_multiple_templates(local_library):
    package = local_library / 'CN' / 'GB55015-2021'
    template = json.loads((package / 'Office_3A.json').read_text(encoding='utf-8'))
    template['BuildingTemplate']['metadata']['id'] = 'CN_GB55015-2021_Office_4A'
    template['BuildingTemplate']['metadata']['climate_zone'] = '4A'
    (package / 'Office_4A.json').write_text(json.dumps(template), encoding='utf-8')
    assert set(building_templates(local_library)) == {
        OFFICE_3A, 'CN/GB55015-2021/4A/Office'}


def test_catalog_supports_multiple_standards_for_one_country(local_library):
    manifest_path = local_library / 'library.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    first_standard = manifest['countries']['CN']['standards']['GB55015-2021']
    manifest['countries']['CN']['standards']['TEST-STD'] = {
        'standard_metadata': {
            'title': 'Test standard',
            'source_url': 'https://example.test/standard',
        },
        'template_assumptions': deepcopy(first_standard['template_assumptions']),
        'operational_metadata': deepcopy(first_standard['operational_metadata']),
    }
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')

    source_package = local_library / 'CN' / 'GB55015-2021'
    test_package = local_library / 'CN' / 'TEST-STD'
    (test_package / 'schedules').mkdir(parents=True)
    template = json.loads((source_package / 'Office_3A.json').read_text(encoding='utf-8'))
    metadata = template['BuildingTemplate']['metadata']
    metadata['id'] = 'CN_TEST-STD_Office_3A'
    metadata['standard_name'] = 'TEST-STD'
    metadata['standard_source'] = 'https://example.test/standard'
    shutil.copyfile(source_package / 'schedules' / 'office.sch',
                    test_package / 'schedules' / 'office.sch')
    (test_package / 'Office_3A.json').write_text(json.dumps(template), encoding='utf-8')

    templates = building_templates(local_library)
    assert set(templates) == {
        OFFICE_3A, 'CN/TEST-STD/3A/Office'}
    assert templates['CN/TEST-STD/3A/Office']['standard']['country'] == 'CN'
    assert templates['CN/TEST-STD/3A/Office']['standard']['id'] == 'TEST-STD'


def test_snapshot_uses_room_groups_and_direct_envelope_values(office_record):
    record = office_record
    assert 'version' not in record
    assert set(record['BuildingTemplate']) == {'Space', 'Envelope'}
    assert set(record['BuildingTemplate']['Space']) == {'default'}
    assert set(record['BuildingTemplate']['Envelope']) == {
        'exterior_wall', 'interior_wall', 'floor', 'ground', 'roof', 'window', 'skylight'}
    wall, _ = resolve_surface_template(record, 'wall', 'outdoors')
    window, _ = resolve_surface_template(record, 'window', 'outdoors')
    assert wall['u_value'] == record['BuildingTemplate']['Envelope']['exterior_wall']['u']['value']
    assert window['u_value'] == record['BuildingTemplate']['Envelope']['window']['u']['value']
    assert window['solar_transmittance'] >= 0


def test_room_selection_and_usage_multiplication(office_record):
    profile = deepcopy(office_record)
    default = profile['BuildingTemplate']['Space'].pop('default')
    profile['BuildingTemplate']['Space'] = {
        'room_a': deepcopy(default),
        'room_b': deepcopy(default),
    }
    profile['BuildingTemplate']['Space']['room_b']['internal_gains']['occupancy_density']['value'] *= 2
    room = _space(settings={'zone_room': 'room_a'})
    values = resolve_zone_settings(room, profile)
    room_a = profile['BuildingTemplate']['Space']['room_a']
    occupancy = room_a['internal_gains']['occupancy_density']['value']
    occupancy_fraction = room_a['internal_gains']['occupancy_fraction']['values']
    np.testing.assert_allclose(values['zone_ppsm'], occupancy * np.asarray(occupancy_fraction))
    np.testing.assert_allclose(
        values['zone_equipment'],
        room_a['internal_gains']['equipment_power_density']['value']
        * np.asarray(room_a['internal_gains']['equipment_usage']['values']))
    room_b = resolve_zone_settings(_space(settings={'zone_room': 'room_b'}), profile)
    assert np.any(np.asarray(values['zone_ppsm']) != room_b['zone_ppsm'])
    with pytest.raises(ValueError, match='set zone_room'):
        resolve_zone_settings(_space(), profile)


def test_nullable_setpoints_and_hvac_operating_hours(office_record):
    values = resolve_zone_settings(_space(), office_record)
    assert np.isnan(np.asarray(values['zone_c_temp'], dtype=float)).any()
    assert np.all(np.isfinite(values['zone_h_temp']))
    availability = np.asarray(values['hvac_enabled'])
    np.testing.assert_array_equal(availability, values['zone_hvac_availability'])
    assert np.all(np.isin(availability, (0, 1)))
    np.testing.assert_array_equal(availability[:24], np.zeros(24))
    np.testing.assert_array_equal(values['zone_h_temp'][:24], np.full(24, 5))


def test_explicit_setpoint_does_not_change_hvac_operating_hours(office_record):
    setpoint = [None] * 8760
    setpoint[12] = 18
    space = _space(settings={'zone_h_temp': setpoint})
    space.explicit_settings = ['zone_h_temp']
    values = resolve_zone_settings(space, office_record)
    assert values['zone_h_temp'][12] == 18
    assert values['zone_h_temp'][11] is None
    np.testing.assert_array_equal(values['hvac_enabled'], values['zone_hvac_availability'])
    assert values['hvac_enabled'][12] == 0


def test_model_snapshot_round_trip_is_self_contained(local_library):
    model = MoosasModel()
    space = _space()
    model.spaceList = [space]
    model.metadata = {'project': {'name': 'schedule snapshot'}}
    apply_building_template(model, OFFICE_3A, template_path=local_library)
    payload = encode_energy_metadata(model)
    restored = MoosasModel()
    restored.spaceList = [deepcopy(space)]
    restored.spaceList[0].settings = {}
    apply_energy_metadata(restored, payload)
    assert restored.spaceList[0].settings == space.settings
    assert restored.metadata == model.metadata
    assert 'version' not in space_template(restored, restored.spaceList[0])


def test_applying_multi_room_template_requires_room_selection(local_library):
    template_file = local_library / 'CN' / 'GB55015-2021' / 'Office_3A.json'
    template = json.loads(template_file.read_text(encoding='utf-8'))
    default = template['BuildingTemplate']['Space'].pop('default')
    template['BuildingTemplate']['Space'] = {
        'outpatient': default,
        'inpatient': deepcopy(default),
    }
    template_file.write_text(json.dumps(template), encoding='utf-8')
    model = MoosasModel()
    model.spaceList = [_space()]
    with pytest.raises(ValueError, match='set zone_room'):
        apply_building_template(model, OFFICE_3A, template_path=local_library)
    assert model.buildingTemplate == {}
    assert model.spaceList[0].settings == {}


@pytest.mark.parametrize('change, message', [
    ('metadata', 'metadata does not match'),
    ('envelope', 'invalid Envelope construction groups'),
    ('schedule_length', 'invalid schedule entry'),
    ('schedule_unit', 'unit does not match'),
    ('schedule_path', 'relative .sch'),
])
def test_invalid_template_catalogs_fail_explicitly(local_library, change, message):
    package = local_library / 'CN' / 'GB55015-2021'
    template_file = package / 'Office_3A.json'
    template = json.loads(template_file.read_text(encoding='utf-8'))
    schedule_file = package / 'schedules' / 'office.sch'
    schedule = json.loads(schedule_file.read_text(encoding='utf-8'))
    if change == 'metadata':
        template['BuildingTemplate']['metadata']['country'] = 'US'
    elif change == 'envelope':
        del template['BuildingTemplate']['Envelope']['ground']
    elif change == 'schedule_length':
        schedule['schedules']['default_equipment_usage']['values'].pop()
        schedule_file.write_text(json.dumps(schedule), encoding='utf-8')
    elif change == 'schedule_unit':
        schedule['schedules']['default_equipment_usage']['unit'] = 'W/m2'
        schedule_file.write_text(json.dumps(schedule), encoding='utf-8')
    elif change == 'schedule_path':
        template['BuildingTemplate']['Space']['default']['internal_gains']['equipment_usage']['file'] = '..\\outside.sch'
    template_file.write_text(json.dumps(template), encoding='utf-8')
    with pytest.raises(ValueError, match=message):
        building_templates(local_library)


def test_schedule_loader_ignores_version_field(local_library):
    schedule_file = local_library / 'CN' / 'GB55015-2021' / 'schedules' / 'office.sch'
    schedule = json.loads(schedule_file.read_text(encoding='utf-8'))
    schedule['version'] = 'unvalidated'
    schedule_file.write_text(json.dumps(schedule), encoding='utf-8')
    assert set(building_templates(local_library)) == {OFFICE_3A}


def test_metadata_registry_and_template_selection(local_library):
    model = MoosasModel()
    model.spaceList = [_space()]
    apply_building_template(model, OFFICE_3A, template_path=local_library)
    payload = json.loads(encode_energy_metadata(model))
    assert payload['schema_version'] == 3
    assert list(payload['building_templates']) == [OFFICE_3A]
    assert all('_building_template' not in s['settings'] for s in payload['spaces'].values())
    with pytest.raises(ValueError, match='metadata version'):
        apply_energy_metadata(model, json.dumps({'schema_version': 2}))


def test_occupant_heat_is_not_a_template_parameter(local_library):
    template_file = local_library / 'CN' / 'GB55015-2021' / 'Office_3A.json'
    template = json.loads(template_file.read_text(encoding='utf-8'))
    template['BuildingTemplate']['Space']['default']['internal_gains']['occupant_heat'] = {
        'mode': 'constant', 'value': 75, 'unit': 'W/person'}
    template_file.write_text(json.dumps(template), encoding='utf-8')
    with pytest.raises(ValueError, match='not a template parameter'):
        building_templates(local_library)


def test_template_snapshot_does_not_require_version_and_rejects_per_space_snapshots(office_record):
    space = _space(settings={'zone_template': OFFICE_3A})
    model = SimpleNamespace(buildingTemplate={OFFICE_3A: deepcopy(office_record)})
    model.buildingTemplate[OFFICE_3A].pop('version', None)
    assert space_template(model, space)['id'] == OFFICE_3A
    space.settings['_building_template'] = {}
    with pytest.raises(ValueError, match='Per-space'):
        space_template(model, space)
