"""Resource loading services for :class:`MoosasModel`."""
from __future__ import annotations

import os
import tempfile
import uuid

from ..utils.tools import path
from .thermal_templates import building_templates


def configure_model_resources(model, schedule_path: str | None = None, template_path: str | None = None):
    """Attach the default building template and schedule library to a model."""
    replace_templates = template_path is not None
    template_path = template_path or os.path.join(path.dataBaseDir, "building_template.json")
    if replace_templates or not model.buildingTemplate:
        model.buildingTemplate = building_templates(template_path)
    if not model.schedule:
        load_schedule(model, schedule_path)
    return model


def _schedule_role_from_name(schedule_name: str) -> str | None:
    lower = str(schedule_name).lower()
    if "occdens" in lower or "occupantdensity" in lower:
        return "zone_ppsm"
    if "occheat" in lower or "occupantheatgain" in lower:
        return "zone_popheat"
    if "equip" in lower or "equipmentheatgain" in lower:
        return "zone_equipment"
    if "light" in lower or "lightingheatgain" in lower:
        return "zone_lighting"
    return None


def rebuild_schedule_index(model):
    """Index loaded schedules by building template type and thermal role."""
    prefix_to_type = {
        "OFF": "OFFICE",
        "RES": "RESIDENTIAL",
        "COM": "COMMERCIAL",
        "SCH": "SCHOOL",
        "HOT": "HOTEL",
    }
    schedule_by_type = {}
    for schedule_name, schedule_value in model.schedule.items():
        if not isinstance(schedule_value, dict):
            continue
        schedule_type = str(schedule_value.get("type", "")).strip().title()
        if schedule_type != "Weekly":
            continue
        prefix = str(schedule_name).split("_", 1)[0].upper()
        type_name = prefix_to_type.get(prefix)
        role = _schedule_role_from_name(schedule_name)
        if type_name and role:
            indexed_roles = schedule_by_type.setdefault(type_name, {})
            existing = indexed_roles.get(role)
            if existing is not None and existing != schedule_name:
                raise ValueError(
                    f"Multiple weekly schedules for {type_name} {role}: "
                    f"{existing!r} and {schedule_name!r}"
                )
            indexed_roles[role] = schedule_name
    model.scheduleByType = schedule_by_type
    return schedule_by_type


def load_schedule(model, schedule_path: str | None = None):
    """Load a schedule library into a model's domain state."""
    if schedule_path is None:
        schedule_path = os.path.join(path.dataBaseDir, "schedule", "office.sch")
    schedule_path = os.path.abspath(schedule_path)
    if not os.path.isfile(schedule_path):
        raise FileNotFoundError(f"Schedule file not found: {schedule_path}")

    loaded = {}
    with open(schedule_path, "r", encoding="utf-8", errors="ignore") as schedule_file:
        for line in schedule_file:
            text = line.strip()
            if not text or text.startswith("!"):
                continue
            parts = [part.strip() for part in text.split(",")]
            if len(parts) < 3:
                continue
            name, mode = parts[0], parts[1].lower()
            if mode == "daily":
                values = parts[2:26]
                if len(values) != 24:
                    raise ValueError(f"Invalid daily schedule row '{name}', expected 24 hourly values.")
                loaded[name] = {"type": "Daily", "value": values}
            elif mode == "weekly":
                values = parts[2:9]
                if len(values) != 7:
                    raise ValueError(f"Invalid weekly schedule row '{name}', expected 7 day references.")
                loaded[name] = {"type": "Weekly", "value": values}
            elif mode == "hourly":
                values = parts[2:]
                if len(values) != 8760:
                    raise ValueError(f"Invalid hourly schedule row '{name}', expected 8760 values.")
                loaded[name] = {"type": "Hourly", "value": values}

    model.schedule.update(loaded)
    model.schedulePath = schedule_path
    rebuild_schedule_index(model)
    return model.schedule


def get_schedule_name(model, template_type: str, field_name: str):
    if template_type is None:
        return None
    return model.scheduleByType.get(str(template_type).upper(), {}).get(field_name)


def write_schedule(model, schedule_path: str | None = None):
    """Serialize a model schedule library, returning the written file path."""
    if schedule_path is None:
        schedule_path = os.path.join(tempfile.gettempdir(), f"moosas-schedule-{uuid.uuid4().hex}.sch")
    schedule_path = os.path.abspath(schedule_path)
    path.checkBuildDir(schedule_path)

    with open(schedule_path, "w", encoding="utf-8") as schedule_file:
        schedule_file.write("! Moosas schedule export\n")
        for name, item in model.schedule.items():
            schedule_type = str(item.get("type", "")).lower()
            values = item.get("value", [])
            expected_count = 24 if schedule_type == "daily" else 7 if schedule_type == "weekly" else None
            if expected_count is None:
                continue
            if len(values) != expected_count:
                raise ValueError(f"{item.get('type')} schedule '{name}' must have {expected_count} values.")
            schedule_file.write(f"{name},{item['type']},{','.join(map(str, values))}\n")
    return schedule_path
