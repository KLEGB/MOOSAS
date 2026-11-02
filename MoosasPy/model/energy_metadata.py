"""Typed energy metadata shared by XML/JSON and RDF model persistence."""
from __future__ import annotations

import json


def _json_value(value):
    if hasattr(value,'tolist'):
        return value.tolist()
    raise TypeError(f'Unsupported energy metadata type: {type(value).__name__}')


def encode_energy_metadata(model):
    elements = {}
    spaces = {}
    for space in list(model.spaceList) + list(model.voidList):
        spaces[str(space.id)] = dict(conditioned=space.conditioned,
            explicit_settings=list(space.explicit_settings),
            settings={k:v for k,v in space.settings.items() if not hasattr(v,'applyToIDF')})
        for element in space.getAllFaces(to_dict=False):
            elements[str(element.Uid)] = dict(settings=element.settings,
                explicit_settings=sorted(element.explicit_settings),
                owners=list(element.space), is_outer=bool(element.isOuter))
    return json.dumps(dict(schema_version=1,energy_settings=getattr(model,'energy_settings',{}),
        schedules=model.schedule,spaces=spaces,elements=elements),ensure_ascii=False,allow_nan=False,default=_json_value)


def apply_energy_metadata(model, value):
    if not value:
        return
    data = json.loads(value)
    if data.get('schema_version') != 1:
        raise ValueError('Unsupported energy model metadata version')
    model.energy_settings = data.get('energy_settings',{})
    model.schedule = data.get('schedules',model.schedule)
    from .resources import rebuild_schedule_index
    rebuild_schedule_index(model)
    for space in list(model.spaceList) + list(model.voidList):
        record = data.get('spaces',{}).get(str(space.id),{})
        space.settings.update(record.get('settings',{}))
        space.conditioned = record.get('conditioned',space.conditioned)
        space.explicit_settings = record.get('explicit_settings',space.explicit_settings)
        for element in space.getAllFaces(to_dict=False):
            record = data.get('elements',{}).get(str(element.Uid),{})
            if 'settings' in record:
                element.settings = dict(record['settings'])
            element.explicit_settings = set(record.get('explicit_settings',element.explicit_settings))
            element.space = record.get('owners',element.space)
            element.isOuter = record.get('is_outer',element.isOuter)
    from .io.gbxml_energy import attach
    attach(model,model.energy_settings.get('external_thermal_model'))
