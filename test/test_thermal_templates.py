from copy import deepcopy
import json
from types import SimpleNamespace

import numpy as np
import pytest

from MoosasPy.model import MoosasModel
from MoosasPy.model.energy_metadata import apply_energy_metadata, encode_energy_metadata
from MoosasPy.model.thermal_templates import (
    DEFAULT_LIBRARY, apply_building_template, building_templates, load_thermal_library,
    resolve_surface_template, template_program, validate_template_snapshot,
)


CHINESE_OFFICE = 'CN/GB55015-2021/public_A_cold/OFFICE'


@pytest.fixture
def international_library(tmp_path):
    """Synthetic packages test the contract, not any foreign national standard."""
    manifest = json.loads((DEFAULT_LIBRARY / 'library.json').read_text(encoding='utf-8'))
    source = DEFAULT_LIBRARY / 'CN' / 'GB55015-2021'
    namespaces = ['ZZ/TEST-2026', 'ZZ/TEST-2027']
    manifest['standards'] = namespaces
    (tmp_path / 'library.json').write_text(json.dumps(manifest), encoding='utf-8')
    for namespace in namespaces:
        country, standard_id = namespace.split('/')
        folder = tmp_path / country / standard_id
        folder.mkdir(parents=True)
        common = json.loads((source / 'common.json').read_text(encoding='utf-8'))
        common['standard'] = dict(country=country, id=standard_id, title='Synthetic test standard')
        common['building_types'] = ['residential.json']
        common['envelopes'] = {
            'climate-1': dict(table='synthetic', wall_u=[.2, .8], window_u=1.5,
                              window_shgc=.4, roof_u=.2, exposed_floor_u=.3,
                              column_selector=dict(parameter='storeys', threshold=8)),
        }
        operation = json.loads((source / 'residential.json').read_text(encoding='utf-8'))
        operation['envelopes'] = ['climate-1']
        operation.pop('operation_overrides')
        operation['operation'].update(zone_infiltration=.12, zone_nightACH=.23, zone_collingEER=4.2)
        operation['operation']['week'] = ['work'] * 4 + ['rest'] * 2 + ['work']
        operation['operation']['profiles'] = {
            role: dict(work=[1.] * 24, rest=[0.] * 24)
            for role in ('occupancy', 'lighting', 'equipment', 'hvac')
        }
        (folder / 'common.json').write_text(json.dumps(common), encoding='utf-8')
        (folder / 'residential.json').write_text(json.dumps(operation), encoding='utf-8')
    return tmp_path


def test_bundled_standard_has_no_selected_or_default_template():
    library = load_thermal_library()
    assert set(library['standards']) == {'CN/GB55015-2021'}
    records = building_templates()
    assert len(records) == 55
    assert all(name.startswith('CN/GB55015-2021/') for name in records)
    assert CHINESE_OFFICE in records
    cold = records['CN/GB55015-2021/residential_cold_A/RESIDENTIAL']
    warm = records['CN/GB55015-2021/residential_mild_A/RESIDENTIAL']
    assert cold['zone_infiltration'] == cold['zone_nightACH'] == .5
    assert warm['zone_infiltration'] == warm['zone_nightACH'] == 1.
    assert cold['zone_collingEER'] == warm['zone_collingEER'] == 3.6


def test_standard_versions_are_namespaced_and_not_inferred(international_library):
    records = building_templates(international_library)
    assert set(records) == {
        'ZZ/TEST-2026/climate-1/RESIDENTIAL', 'ZZ/TEST-2027/climate-1/RESIDENTIAL'}
    record = records['ZZ/TEST-2026/climate-1/RESIDENTIAL']
    assert record['standard'] == 'ZZ/TEST-2026'
    assert record['zone_infiltration'] == .12
    assert record['zone_nightACH'] == .23
    assert record['zone_collingEER'] == 4.2
    resolved, _ = resolve_surface_template(record, 'wall', 'outdoors', (0, 1, 0), context={'storeys': 7})
    assert resolved['u_value'] == .2
    resolved, _ = resolve_surface_template(record, 'wall', 'outdoors', (0, 1, 0), context={'storeys': 9})
    assert resolved['u_value'] == .8
    with pytest.raises(ValueError, match='Unknown building template'):
        apply_building_template(MoosasModel(), CHINESE_OFFICE, template_path=international_library)


def test_week_is_defined_by_standard_not_global_weekend(international_library):
    record = building_templates(international_library)['ZZ/TEST-2026/climate-1/RESIDENTIAL']
    values = template_program(record, 'zone_ppsm', 2.)
    assert values.shape == (8760,)
    expected = np.concatenate([np.full(24, value) for value in (2, 2, 2, 2, 0, 0, 2)])
    np.testing.assert_array_equal(values[:168], expected)
    np.testing.assert_array_equal(values[168:336], expected)


@pytest.mark.parametrize('change, message', [
    ('projection', 'Unsupported template projection'),
    ('rule', 'unsupported envelope rules'),
    ('scope', 'unsupported wwr_scope'),
    ('week', 'unknown day profile'),
    ('unit', 'SI units'),
    ('metadata', 'metadata does not match'),
    ('duplicate', 'Duplicate standard namespace'),
])
def test_invalid_packages_fail_explicitly(international_library, change, message):
    folder = international_library / 'ZZ' / 'TEST-2026'
    common_file = folder / 'common.json'
    operation_file = folder / 'residential.json'
    common = json.loads(common_file.read_text(encoding='utf-8'))
    operation = json.loads(operation_file.read_text(encoding='utf-8'))
    if change == 'projection':
        common['projection'] = 'unsupported-national-calculation'
    elif change == 'rule':
        common['envelopes']['climate-1']['unknown_national_rule'] = True
    elif change == 'scope':
        operation['wwr_scope'] = 'district'
    elif change == 'week':
        operation['operation']['week'][0] = 'missing'
    elif change == 'unit':
        common['unit_system'] = 'IP'
    elif change == 'metadata':
        common['standard']['country'] = 'CN'
    elif change == 'duplicate':
        manifest_file = international_library / 'library.json'
        manifest = json.loads(manifest_file.read_text(encoding='utf-8'))
        manifest['standards'].append('ZZ/TEST-2026')
        manifest_file.write_text(json.dumps(manifest), encoding='utf-8')
    common_file.write_text(json.dumps(common), encoding='utf-8')
    operation_file.write_text(json.dumps(operation), encoding='utf-8')
    with pytest.raises(ValueError, match=message):
        building_templates(international_library)


def test_explicit_application_and_self_contained_snapshot(international_library):
    template_id = 'ZZ/TEST-2026/climate-1/RESIDENTIAL'
    space = SimpleNamespace(id='z1', settings={'zone_template': None},
                            explicit_settings=[], conditioned=True, getAllFaces=lambda **_: [])
    model = MoosasModel()
    model.spaceList = [space]
    apply_building_template(model, template_id, template_path=international_library)
    payload = encode_energy_metadata(model)
    # Restoring and evaluating the expanded snapshot does not read the library.
    (international_library / 'library.json').unlink()
    restored = MoosasModel()
    restored.spaceList = [deepcopy(space)]
    restored.spaceList[0].settings = {}
    apply_energy_metadata(restored, payload)
    assert restored.spaceList[0].settings == space.settings
    assert template_program(restored.spaceList[0].settings, 'zone_ppsm', 1.)[96] == 0.


def test_legacy_or_mismatched_snapshot_is_not_silently_reinterpreted():
    with pytest.raises(ValueError, match='explicitly reapply'):
        validate_template_snapshot({'_thermal_template': dict(id='legacy', operation={})})
    record = building_templates()[CHINESE_OFFICE]
    record['zone_template'] = 'ZZ/TEST-2026/climate-1/OFFICE'
    with pytest.raises(ValueError, match='does not match'):
        template_program(record, 'zone_ppsm', 1.)


def test_snapshot_rejects_unsupported_wwr_scope():
    record = building_templates()[CHINESE_OFFICE]
    record['_thermal_template']['wwr_scope'] = 'district'
    with pytest.raises(ValueError, match='Unsupported template wwr_scope'):
        validate_template_snapshot(record)
