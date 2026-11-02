"""Coupled energy and airflow simulation workflows."""

from ..energy.runner import EnergyRunner, build_energy_input
from ...transform.geometry.geos import Projection, Vector, Ray
from ...model import MoosasModel
from ...model.resources import get_schedule_name, load_schedule, write_schedule
from ..weather import CumulativeSky, WeatherData, build_cumulative_skies
from ..radiation import calculate_model_radiation, ray_test, write_radiation_geometry
from ...utils import np, path, os
from ...utils.date import DateTime
from ...utils.constant import afn
from ..airflow.network import (
    NETWORK_SCHEMA_VERSION,
    AfnNetwork,
    AfnPath,
    AfnZone,
)
from ..airflow.runner import (
    AirflowRunner,
    AirflowZoneResult,
    VentPaths,
    read_path_result,
    _write_project_temperatures,
)
import networkx as nx
from copy import deepcopy
from dataclasses import replace
from ..workspace import SimulationWorkspace
from ..airflow.balance import BalanceModel, BalanceState, AirflowThermalBoundary
import shapely
import csv
import json
import time
import uuid
from collections import deque


def _linear_interpolate_nan_series(values):
    arr = np.array(values, dtype=float)
    if arr.size == 0:
        return []
    x = np.arange(arr.size)
    mask = np.isfinite(arr)
    if not mask.any() or mask.all():
        return arr.tolist()
    return np.interp(x, x[mask], arr[mask]).tolist()


class EnergyAirflowCoupler(object):
    __slots__ = [
        'model', 'zones', 'paths', 'networkDict', 'skySeries', 'pathRadIntensity', 'runtime',
        'schedulePath', '_sch_daily_map', '_sch_weekly_map', '_sch_loaded_names', 'weather'
    ]
    ENERGY_INDEX = ["space_height", "zone_area", "outside_area", "facade_area", "window_area", "roof_area",
                    "skylight_area", "floor_area",
                    "summer_solar", "winter_solar", "zone_wallU", "zone_winU", "zone_win_SHGC", "zone_c_temp",
                    "zone_c_hum", "zone_h_temp",
                    "zone_collingEER", "zone_HeatingEER", "zone_work_start", "zone_work_end", "zone_ppsm", "zone_pfav",
                    "zone_popheat",
                    "zone_equipment", "zone_lighting", "zone_infiltration", "zone_nightACH", "zone_name",
                    "zone_summerrad", "zone_winterrad", "zone_template"
                    ]
    AFN_INDEX = ['userName', 'temperature', 'prjIndex', 'prjName', 'heatLoad', 'volume', 'position_x', 'position_y',
                 'position_z', 'boundary', 'level', 'levelIndex', 'relHt', 'contam']
    LPG_OUTSIDE_NODE = "OUTSIDE"
    LPG_ZONE_FIELDS = ("zone_wallU", "zone_winU", "zone_win_SHGC")
    LPG_PATH_FIELDS = ("pathHeight", "pathWidth", "pressure", "pathType")

    def __init__(self, model: MoosasModel, weather: WeatherData,
                 cumulative_sky_matrix, schedulePath=None):
        print("-----------------------\nPrepareing network...\n-----------------------")
        import time
        t0 = time.time()
        self.model = model
        self.schedulePath = schedulePath
        self._sch_daily_map = {}
        self._sch_weekly_map = {}
        self._sch_loaded_names = set()
        if self.schedulePath is not None:
            load_schedule(self.model, self.schedulePath)
        elif getattr(self.model, "schedule", None):
            self.schedulePath = write_schedule(self.model)
        self._parse_schedule_file()
        self.weather = weather
        cumulative_sky_matrix = np.asarray(cumulative_sky_matrix, dtype=float)
        cumulative_sky = build_cumulative_skies(cumulative_sky_matrix)
        calculate_model_radiation(self.model, cumulative_sky, reflection=0)
        network = AfnNetwork(self.model)
        self.zones = network.zones
        self.paths = network.paths
        self.networkDict = self.buildNetwork(network)
        self.pathRadIntensity = {ps.userName: [] for ps in self.paths}
        self.runtime = {}
        print("-----------------------\nPrepareing cumSky series...\n-----------------------")
        print(time.time() - t0)
        t0 = time.time()
        self.skySeries = []
        for i in range(8760):
            self.skySeries.append(
                CumulativeSky(cumulative_sky_matrix[:, i] / CumulativeSky.RADIATION_SCALE)
            )

        print("-----------------------\nCalculating path radiation intensity...\n-----------------------")
        print(time.time() - t0)
        t0 = time.time()
        rays = []
        fixMatrix = []
        ray_paths = []
        for ps in self.paths:
            if self._is_leakage_path_dict(ps.toDict()):
                self.pathRadIntensity[ps.userName] = [0.0] * 8760
                continue
            origin = Vector(ps.position_x, ps.position_y, ps.position_z)
            thisRays = [Ray(origin, pos) for pos in self.skySeries[0].positions]
            rays += thisRays
            fixMatrix.append(np.array([abs(Vector.dot(ps.geometry.normal, r.direction)) for r in thisRays]))
            ray_paths.append(ps)
        if ray_paths:
            rays = np.array(rays)
            geo_path = write_radiation_geometry(model)
            resRay = ray_test(rays, geo_path=geo_path)
            resRay = np.array([1.0 if r is not None else 0 for r in resRay])
            resRay = resRay.reshape(len(ray_paths), int(len(resRay) / len(ray_paths)))

        for i, ps in enumerate(ray_paths):
            for sky in self.skySeries:
                self.pathRadIntensity[ps.userName].append(np.sum(resRay[i] * sky.values * fixMatrix[i]) * 1000)
        print("-----------------------\nNetwork Ready...\n-----------------------")
        print(time.time() - t0)
        t0 = time.time()


    def _parse_schedule_file(self):
        self._sch_daily_map = {}
        self._sch_weekly_map = {}
        self._sch_loaded_names = set()
        if self.schedulePath is None:
            return
        with open(self.schedulePath, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                text = line.strip()
                if (not text) or text.startswith("!"):
                    continue
                parts = [p.strip() for p in text.split(",")]
                if len(parts) < 3:
                    continue
                name = parts[0]
                mode = parts[1].strip().lower()
                self._sch_loaded_names.add(name)
                if mode == "daily":
                    values = parts[2:26]
                    if len(values) != 24:
                        raise ValueError(f"Invalid daily schedule row '{name}', expected 24 hourly values.")
                    try:
                        self._sch_daily_map[name] = [float(v) for v in values]
                    except Exception as e:
                        raise ValueError(f"Invalid numeric value in daily schedule '{name}': {e}")
                elif mode == "weekly":
                    values = parts[2:9]
                    if len(values) != 7:
                        raise ValueError(f"Invalid weekly schedule row '{name}', expected 7 daily schedule references.")
                    self._sch_weekly_map[name] = [str(v) for v in values]

    def _value_from_schedule(self, schedule_name, hoy):
        if schedule_name in self._sch_weekly_map:
            week_refs = self._sch_weekly_map[schedule_name]
            dt = DateTime.from_hoy(float(hoy))
            daily_name = week_refs[dt.weekday()]
            if daily_name not in self._sch_daily_map:
                raise ValueError(
                    f"Weekly schedule '{schedule_name}' references missing daily schedule '{daily_name}'."
                )
            return self._sch_daily_map[daily_name][dt.hour]
        if schedule_name in self._sch_daily_map:
            dt = DateTime.from_hoy(float(hoy))
            return self._sch_daily_map[schedule_name][dt.hour]
        raise KeyError(schedule_name)

    def _resolve_gain_value(self, raw_value, hoy, field_name, zone_name):
        if raw_value is None:
            return 0.0
        if isinstance(raw_value, str) and raw_value.strip().lower() in ("", "none", "null", "nan"):
            return 0.0
        try:
            return float(raw_value)
        except Exception:
            pass
        try:
            # Resolve schedule using current hour-of-year so gains follow
            # hourly daily/weekly schedules instead of a fixed midnight value.
            return float(self._value_from_schedule(str(raw_value), hoy))
        except KeyError:
            raise ValueError(
                f"Schedule '{raw_value}' for zone '{zone_name}' field '{field_name}' not found in "
                f"loaded schedule library."
            )

    def updateHeatLoad(self, hoy, energyDict: dict = None):
        """
        Update per-zone sensible heat load for a given hour of year.

        Parameters
        ----------
        hoy : int
            Hour-of-year index.
        energyDict : dict, optional
            Network dictionary override.

        Returns
        -------
        dict
            Updated `networkDict`.
        """

        if energyDict:
            self.networkDict = energyDict
        def _safe_float(value, default=0.0):
            if value is None:
                return default
            if isinstance(value, str) and value.strip().lower() in ("", "none", "null", "nan"):
                return default
            try:
                return float(value)
            except Exception:
                return default

        prjDict = {}
        for zUserName, zValue in self.networkDict['zones'].items():
            zone_area = _safe_float(zValue.get("zone_area"))
            zone_ppsm = self._resolve_gain_value(zValue.get('zone_ppsm'), hoy, 'zone_ppsm', zUserName)
            zone_equipment = self._resolve_gain_value(zValue.get('zone_equipment'), hoy, 'zone_equipment', zUserName)
            zone_lighting = self._resolve_gain_value(zValue.get('zone_lighting'), hoy, 'zone_lighting', zUserName)
            zone_popheat = _safe_float(zValue.get('zone_popheat'))
            ppsm_heat = zone_ppsm * zone_popheat * zone_area
            equipment_heat = zone_equipment * zone_area
            lighting_heat = zone_lighting * zone_area
            internal_heat = ppsm_heat + equipment_heat + lighting_heat
            self.networkDict['zones'][zUserName]['zone_ppsm_heat'] = ppsm_heat
            self.networkDict['zones'][zUserName]['zone_equipment_heat'] = equipment_heat
            self.networkDict['zones'][zUserName]['zone_lighting_heat'] = lighting_heat
            self.networkDict['zones'][zUserName]['zone_transmitted_solar_w'] = 0.0
            self.networkDict['zones'][zUserName]['zone_radHeat'] = 0.0
            self.networkDict['zones'][zUserName]['heatLoad'] = internal_heat
            idx = self.networkDict['zones'][zUserName]['prjIndex']
            prjDict[idx] = zUserName
            prjDict[str(idx)] = zUserName

        path_geometry = {str(path.userName): getattr(path, 'geometry', None) for path in self.paths}
        for ps in self.networkDict['paths'].values():
            if self._is_leakage_path_dict(ps):
                continue
            # Keep AFN heatload power baseline fixed at the initial hour.
            pid = ps["userName"]
            if pid not in self.pathRadIntensity:
                self.pathRadIntensity[pid] = [0.0] * 8760
            from_zone = self._coerce_zone_ref(ps.get("fromZone"))
            to_zone = self._coerce_zone_ref(ps.get("toZone"))
            # Direct solar is admitted only through an exterior transparent
            # opening.  Inter-zone openings and leakage paths have no outdoor
            # transmitted-solar term.
            if (from_zone == -1) == (to_zone == -1):
                continue
            zUserName = prjDict.get(to_zone if from_zone == -1 else from_zone)
            if zUserName is None:
                continue
            glazing = path_geometry.get(str(pid))
            if glazing is not None:
                glazing_area = float(glazing.area)
                shgc = float(getattr(glazing, 'SHGC', 0.0))
            else:
                glazing_area = float(ps['pathHeight']) * float(ps['pathWidth'])
                shgc = float((ps.get('element') or {}).get('shgc', 0.0))
            radHeat = self.pathRadIntensity[pid][hoy] * max(0.0, glazing_area) * max(0.0, shgc)
            self.networkDict['zones'][zUserName]['zone_transmitted_solar_w'] += radHeat
            # Keep the legacy field and total load semantically aligned: both
            # now represent transmitted, SHGC-adjusted solar heat.
            self.networkDict['zones'][zUserName]['zone_radHeat'] += radHeat
            self.networkDict['zones'][zUserName]['heatLoad'] += radHeat
            
        return self.networkDict

    def buildNetwork(self, network):
        """
        Build merged ventilation-energy network dictionary.

        Parameters
        ----------
        network : AfnNetwork
            Airflow network object.

        Returns
        -------
        dict
            Combined network dictionary.
        """

        # network.checkTopology()
        # raise Exception
        # for z in network.zones:
        #     print(z.printHeatLoad())
        zoneDict = {z.userName: z.toDict() for z in network.zones}
        pathDict = {p.userName: p.toDict() for p in network.paths}
        energyDict = build_energy_input(self.model, self.weather, require_radiation=True)
        for z in energyDict['zones']:
            for key in z.params.keys():
                zone_name = z.params.get('zone_name')
                zone_id = getattr(z, 'id', None)
                if zone_name in zoneDict:
                    zoneDict[zone_name][key] = z.params[key]
                elif zone_id in zoneDict:
                    zoneDict[zone_id][key] = z.params[key]
        energyDict['paths'] = pathDict
        energyDict['zones'] = zoneDict
        energyDict['schemaVersion'] = NETWORK_SCHEMA_VERSION
        energyDict['levels'] = [dict(level) for level in network.levels]
        return energyDict

    @staticmethod
    def _coerce_zone_ref(value):
        if value is None:
            return -1
        return int(round(float(value)))

    @staticmethod
    def _energy_zone_template_type(zone_dict: dict) -> str:
        template = str(zone_dict.get("zone_template", "")).strip()
        if not template:
            return ""
        return template.split("_")[-1].upper()

    def _resolve_energy_schedule_ref(self, zone_dict: dict, field_name: str):
        raw_value = zone_dict.get(field_name)
        if raw_value is None:
            return raw_value
        schedule_lib = getattr(self.model, "schedule", {})
        active_schedule_names = set(self._sch_daily_map) | set(self._sch_weekly_map)
        if isinstance(raw_value, str):
            text = raw_value.strip()
            if text == "":
                return text
            try:
                return float(text)
            except Exception:
                pass
            if text in active_schedule_names or (not active_schedule_names and text in schedule_lib):
                return text

        template_type = self._energy_zone_template_type(zone_dict)
        if template_type:
            schedule_name = get_schedule_name(self.model, template_type, field_name)
            if schedule_name in active_schedule_names or (not active_schedule_names and schedule_name in schedule_lib):
                return schedule_name
        candidate_pool = active_schedule_names if active_schedule_names else set(schedule_lib.keys())
        candidates = [
            name for name in candidate_pool
            if _schedule_role_from_name(name) == field_name
        ]
        for preferred_token in ("weekly", "allday"):
            for name in sorted(candidates):
                if preferred_token in name.lower():
                    return name
        if candidates:
            return sorted(candidates)[0]
        for schedule_map in getattr(self.model, "scheduleByType", {}).values():
            if not isinstance(schedule_map, dict):
                continue
            schedule_name = schedule_map.get(field_name)
            if schedule_name in active_schedule_names or (not active_schedule_names and schedule_name in schedule_lib):
                return schedule_name
        return raw_value

    def _normalize_energy_schedule_fields_in_dict(self, energy_dict: dict = None):
        if not isinstance(energy_dict, dict):
            return energy_dict
        for zone_dict in energy_dict.get("zones", {}).values():
            for field_name in ("zone_ppsm", "zone_equipment", "zone_lighting"):
                zone_dict[field_name] = self._resolve_energy_schedule_ref(zone_dict, field_name)
        return energy_dict

    @staticmethod
    def _normalize_energy_cli_arg(arg):
        if not isinstance(arg, str):
            return arg
        text = arg.strip()
        if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
            return text[1:-1]
        return text

    def reconstructEnergyInputs(self, energyDict: dict = None):
        """
        Reconstruct thermal energy-analysis inputs from network dictionary.

        Parameters
        ----------
        energyDict : dict, optional
            Network dictionary override.

        Returns
        -------
        dict
            Energy input structure for `EnergyRunner`.
        """
        if energyDict:
            self.networkDict = self._normalize_energy_schedule_fields_in_dict(energyDict)
        energyDict = self.networkDict
        overrides = {}
        for key, zone in energyDict['zones'].items():
            name = str(zone.get('userName', key))
            overrides[name] = {field: zone[field] for field in self.ENERGY_INDEX if field in zone}
            for field in ('zone_ppsm','zone_equipment','zone_lighting'):
                overrides[name][field] = self._resolve_energy_schedule_ref(zone,field)
        return build_energy_input(self.model,self.weather,zone_ids=tuple(overrides),zone_overrides=overrides)

    def reconstructVentilationInputs(self, energyDict: dict = None):
        """
        Reconstruct AFN zones and paths objects from network dictionary.

        Parameters
        ----------
        energyDict : dict, optional
            Network dictionary override.

        Returns
        -------
        dict
            `{\"paths\": list[AfnPath], \"zones\": list[AfnZone]}`.
        """
        if energyDict:
            self.networkDict = self._normalize_energy_schedule_fields_in_dict(energyDict)
        else:
            self._normalize_energy_schedule_fields_in_dict(self.networkDict)
        energyDict = self.networkDict
        paths = [AfnPath(**pathDict) for pathDict in energyDict['paths'].values()]
        zones = [{key: zoneDict[key] for key in self.AFN_INDEX} for zoneDict in energyDict['zones'].values()]
        zones = [AfnZone(**zoneDict) for zoneDict in zones]
        return {"paths": paths, "zones": zones}

    def networkDict_to_lpg(self, energyDict: dict = None):
        """
        Convert network dictionary to a zone-node/path-edge LPG.

        Returns
        -------
        nx.MultiDiGraph
            Zone nodes with selected zone attributes and directed path edges.
        """
        if energyDict is not None:
            self.networkDict = energyDict
        energyDict = self.networkDict

        graph = nx.MultiDiGraph()
        graph.add_node(self.LPG_OUTSIDE_NODE)

        zones = energyDict.get("zones", {})
        paths = energyDict.get("paths", {})

        prj_to_node = {}
        for zone_key, zone in zones.items():
            node_id = zone.get("userName", zone_key)
            prj_idx = str(zone.get("prjIndex"))
            prj_to_node[prj_idx] = node_id
            graph.add_node(
                node_id,
                zone_wallU=zone.get("zone_wallU"),
                zone_winU=zone.get("zone_winU"),
                zone_win_SHGC=zone.get("zone_win_SHGC"),
            )

        for path_key, path_value in paths.items():
            if self._is_leakage_path_dict(path_value):
                continue
            from_zone = str(path_value.get("fromZone"))
            to_zone = str(path_value.get("toZone"))
            if from_zone == "-1":
                u = self.LPG_OUTSIDE_NODE
            else:
                if from_zone not in prj_to_node:
                    raise ValueError(
                        f"Path '{path_key}' has fromZone='{from_zone}' but no matching zone.prjIndex."
                    )
                u = prj_to_node[from_zone]
            if to_zone == "-1":
                v = self.LPG_OUTSIDE_NODE
            else:
                if to_zone not in prj_to_node:
                    raise ValueError(
                        f"Path '{path_key}' has toZone='{to_zone}' but no matching zone.prjIndex."
                    )
                v = prj_to_node[to_zone]
            edge_key = path_value.get("userName", path_key)
            graph.add_edge(
                u,
                v,
                key=edge_key,
                pathHeight=path_value.get("pathHeight"),
                pathWidth=path_value.get("pathWidth"),
                pressure=path_value.get("pressure"),
                pathType=path_value.get("pathType"),
            )
        return graph

    def lpg_to_networkDict(self, graph: nx.MultiDiGraph, base_networkDict: dict = None):
        """
        Apply edited LPG attributes back into network dictionary.

        Only whitelisted fields are written back.
        """
        if not isinstance(graph, nx.MultiDiGraph):
            raise TypeError("graph must be an instance of nx.MultiDiGraph.")

        if base_networkDict is None:
            base_networkDict = self.networkDict
        networkDict = deepcopy(base_networkDict)

        zones = networkDict.get("zones", {})
        paths = networkDict.get("paths", {})

        zone_node_to_key = {}
        for zone_key, zone_value in zones.items():
            node_id = zone_value.get("userName", zone_key)
            zone_node_to_key[node_id] = zone_key

        path_id_to_key = {}
        for path_key, path_value in paths.items():
            path_id_to_key[path_key] = path_key
            user_name = path_value.get("userName")
            if user_name is not None:
                path_id_to_key[user_name] = path_key

        for node_id, node_attrs in graph.nodes(data=True):
            if node_id == self.LPG_OUTSIDE_NODE:
                continue
            zone_key = zone_node_to_key.get(node_id)
            if zone_key is None:
                continue
            for field in self.LPG_ZONE_FIELDS:
                if field in node_attrs:
                    zones[zone_key][field] = node_attrs[field]

        for _, _, edge_key, edge_attrs in graph.edges(keys=True, data=True):
            path_key = path_id_to_key.get(edge_key)
            if path_key is None:
                continue
            for field in self.LPG_PATH_FIELDS:
                if field in edge_attrs:
                    paths[path_key][field] = edge_attrs[field]

        # Recompute zone.window_area from original outside-linked paths only.
        # Rule requested:
        # - sum area for paths linked with '-1'
        # - exclude newly added toZone='-1' paths
        zone_by_prj = {}
        for zone_key, zone_value in zones.items():
            zone_by_prj[str(zone_value.get("prjIndex"))] = zone_key
            zones[zone_key]["window_area"] = 0.0

        def _is_newly_added_outside_path(path_obj: dict) -> bool:
            user_name = str(path_obj.get("userName", ""))
            path_id = str(path_obj.get("prjIndex", ""))
            # New optimization paths follow cem_* naming in BiCEM pipeline.
            return (
                user_name.startswith("cem_")
                or path_id.startswith("cem_")
            )

        for path_value in paths.values():
            if self._is_leakage_path_dict(path_value):
                continue
            from_zone = str(path_value.get("fromZone", ""))
            to_zone = str(path_value.get("toZone", ""))
            is_outside_link = (from_zone == "-1" and to_zone != "-1") or (to_zone == "-1" and from_zone != "-1")
            if not is_outside_link:
                continue
            if to_zone == "-1" and _is_newly_added_outside_path(path_value):
                continue
            zone_prj = to_zone if from_zone == "-1" else from_zone
            zone_key = zone_by_prj.get(zone_prj)
            if zone_key is None:
                continue
            try:
                area = float(path_value.get("pathHeight", 0.0)) * float(path_value.get("pathWidth", 0.0))
            except Exception:
                area = 0.0
            zones[zone_key]["window_area"] = float(zones[zone_key].get("window_area", 0.0)) + area

        self.networkDict = networkDict
        return networkDict

    def energyTask(self, energyDict: dict = None):
        """
        Run one thermal-energy analysis task.

        Parameters
        ----------
        energyDict : dict, optional
            Network dictionary override.

        Returns
        -------
        dict
            {
              "zone_name": {
                "zone_area": ...,
                "heating": [...], "cooling": [...], "Lighting": [...],              # daily (365)
                "heating_hourly": [...], "cooling_hourly": [...], "Lighting_hourly": [...]  # hourly (8760)
              }
            }
        """
        if energyDict:
            self.networkDict = energyDict

        zoneIndexDict = {}
        for z in self.networkDict['zones'].keys():
            self.networkDict['zones'][z]['zone_summerrad'] = 0
            self.networkDict['zones'][z]['zone_winterrad'] = 0
            zoneIndexDict[self.networkDict['zones'][z]['prjIndex']] = z

        for ps in self.networkDict['paths'].values():
            if self._is_leakage_path_dict(ps):
                continue
            pid = ps["userName"]
            if pid not in self.pathRadIntensity:
                self.pathRadIntensity[pid] = [0.0] * 8760
            summer_intensity = np.sum(self.pathRadIntensity[pid][CumulativeSky.SUMMER_START_HOUR:CumulativeSky.SUMMER_END_HOUR])
            winter_intensity = np.sum(self.pathRadIntensity[pid][CumulativeSky.WINTER_START_HOUR:]) + \
                               np.sum(self.pathRadIntensity[pid][:CumulativeSky.WINTER_END_HOUR])
            summerradHeat = summer_intensity * float(ps['pathHeight']) * float(ps['pathWidth'])
            winterradHeat = winter_intensity * float(ps['pathHeight']) * float(ps['pathWidth'])
            from_zone = self._coerce_zone_ref(ps.get("fromZone"))
            to_zone = self._coerce_zone_ref(ps.get("toZone"))
            if from_zone != -1:
                zUserName = zoneIndexDict.get(from_zone)
                if zUserName is not None:
                    self.networkDict['zones'][zUserName]['zone_summerrad'] += summerradHeat
                    self.networkDict['zones'][zUserName]['zone_winterrad'] += winterradHeat
            if to_zone != -1:
                zUserName = zoneIndexDict.get(to_zone)
                if zUserName is not None:
                    self.networkDict['zones'][zUserName]['zone_summerrad'] += summerradHeat
                    self.networkDict['zones'][zUserName]['zone_winterrad'] += winterradHeat
        
        energyDict = self.networkDict
        energyInput = self.reconstructEnergyInputs(energyDict)
        e_data = EnergyRunner(
            weather=self.weather,
            energy_input=energyInput,
            temporal_scale="hourly",
            spatial_scale="zone",
        ).run().data
        return self._format_energy_result(energyInput, e_data)

    @staticmethod
    def _fit_series(values, size):
        vals = list(values)[:size]
        if len(vals) < size:
            vals += [np.nan] * (size - len(vals))
        return vals

    def _format_energy_result(self, energyInput, e_data):
        zone_days = e_data.get("zone_days", [])
        zone_hours = e_data.get("zone_hours", [])
        result = {}
        for i, z in enumerate(energyInput.zones):
            zone_name = z.zone_id
            zone_area = z.area_m2
            hours = zone_hours[i] if i < len(zone_hours) else []
            days = zone_days[i] if i < len(zone_days) else [
                {
                    key: sum(float(hour[key]) for hour in hours[start:start + 24])
                    for key in ("heating", "cooling", "lighting")
                }
                for start in range(0, len(hours), 24)
            ]
            heating = self._fit_series([float(d["heating"]) for d in days], 365)
            cooling = self._fit_series([float(d["cooling"]) for d in days], 365)
            lighting = self._fit_series([float(d["lighting"]) for d in days], 365)
            heating_hourly = self._fit_series([float(h["heating"]) for h in hours], 8760)
            cooling_hourly = self._fit_series([float(h["cooling"]) for h in hours], 8760)
            lighting_hourly = self._fit_series([float(h["lighting"]) for h in hours], 8760)
            result[zone_name] = {
                "zone_area": zone_area,
                "heating": heating,
                "cooling": cooling,
                "Lighting": lighting,
                "heating_hourly": heating_hourly,
                "cooling_hourly": cooling_hourly,
                "Lighting_hourly": lighting_hourly
            }
        return result

    @staticmethod
    def _window_mean(arr, st, ed):
        if st >= ed:
            return 0.0
        vals = np.array(arr[st:ed], dtype=float)
        if vals.size == 0:
            return 0.0
        return float(np.mean(vals))

    @staticmethod
    def _to_float(value, default=0.0):
        if value is None:
            return float(default)
        if isinstance(value, str) and value.strip().lower() in ("", "none", "null", "nan"):
            return float(default)
        try:
            return float(value)
        except Exception:
            return float(default)

    def _mechanical_candidate_paths(self):
        candidates = []
        for path_key, p in self.networkDict.get("paths", {}).items():
            if self._is_leakage_path_dict(p):
                continue
            from_zone = str(p.get("fromZone", ""))
            to_zone = str(p.get("toZone", ""))
            is_outside_edge = (from_zone == "-1" and to_zone != "-1") or (to_zone == "-1" and from_zone != "-1")
            if not is_outside_edge:
                continue
            # Mechanical path identification should be strict.
            # Do not treat all outside-linked pressure paths as mechanical,
            # otherwise natural ventilation openings are over-counted as fan power.
            user_name = str(p.get("userName", path_key)).lower()
            path_type = str(p.get("pathType", "")).lower()
            is_mechanical = (
                bool(p.get("is_mechanical", False))
                or user_name.startswith("cem_mech_")
                or ("mech" in user_name)
                or ("fan" in user_name)
                or (path_type == "mechanical")
            )
            if not is_mechanical:
                continue
            indoor_prj = to_zone if from_zone == "-1" else from_zone
            candidates.append((str(p.get("userName", path_key)), str(indoor_prj)))
        return candidates

    def _zone_occupancy_switch(self, hoy):
        occ = {}
        for zone_key, z in self.networkDict.get("zones", {}).items():
            zone_user = str(z.get("userName", zone_key))
            ppsm = self._resolve_gain_value(z.get("zone_ppsm"), hoy, "zone_ppsm", zone_user)
            occ[zone_user] = bool(float(ppsm) > 0.0)
        return occ

    def _mechanical_energy_kwh_m2_by_zone(self, prj_file, hoy, specific_power=0.27):
        mech_energy = {}
        if (not prj_file) or (not os.path.exists(prj_file)):
            return mech_energy

        mech_paths = self._mechanical_candidate_paths()
        if len(mech_paths) == 0:
            return mech_energy

        flow_by_path = {}
        try:
            flow_result = read_path_result(prj_file)
            path_list = list(self.networkDict.get("paths", {}).values())
            for i, p in enumerate(path_list):
                if i not in flow_result:
                    continue
                pid = str(p.get("userName", f"path_{i}"))
                flow_by_path[pid] = self._to_float(flow_result[i].get("flow"), 0.0)
        except Exception:
            return mech_energy

        zones = self.networkDict.get("zones", {})
        prj_to_zone_user = {
            str(z.get("prjIndex")): str(z.get("userName", zone_key))
            for zone_key, z in zones.items()
        }
        zone_user_to_info = {
            str(z.get("userName", zone_key)): z
            for zone_key, z in zones.items()
        }
        occ = self._zone_occupancy_switch(hoy)

        for path_id, indoor_prj in mech_paths:
            zone_user = prj_to_zone_user.get(str(indoor_prj))
            if zone_user is None:
                continue
            if not occ.get(zone_user, False):
                continue
            zone_area = self._to_float(zone_user_to_info.get(zone_user, {}).get("zone_area"), 0.0)
            if zone_area <= 0:
                continue
            flow_m3h = abs(self._to_float(flow_by_path.get(path_id), 0.0))
            energy_wh = float(specific_power) * flow_m3h
            mech_energy[zone_user] = mech_energy.get(zone_user, 0.0) + (energy_wh / 1000.0 / zone_area)
        return mech_energy

    def coupledTask(self, energyDict: dict = None, timestep=1, iteration=1,
                    k=0.8, sigma=3.8, start_hoy=5088, end_hoy=5112,
                    earse_conditioned=False, mode="sequence", preheat=afn.PREHEAT_ROUNDS, **kwargs):
        """
        Coupled simulation between energy and comfort results.

        Parameters
        ----------
        start_hoy : int, default 5088
            Inclusive start hour-of-year for coupling simulation.
        end_hoy : int, default 5112
            Inclusive end hour-of-year for coupling simulation.

        Returns
        -------
        dict
            {
              "zone_name": {
                "hoy": [...],
                "zone_area": ...,
                "heating": [...],
                "cooling": [...],
                "lighting": [...],
                "total_energy": [...],
                "total_energy_vent": [...],
                "Temperature": [...],
                "ACH": [...],
                "Comfort": [...],
                "delta_t": [...]
              }
            }
        """
        e_res = self.energyTask(energyDict=energyDict)
        legacy_w = kwargs.pop("w", None)
        if kwargs:
            raise TypeError(f"Unexpected keyword arguments: {', '.join(kwargs.keys())}")
        if legacy_w is not None:
            sigma = legacy_w

        c_res = self.annualComfort(
            energyDict=energyDict,
            timestep=timestep,
            iteration=iteration,
            k=k,
            sigma=sigma,
            start_hoy=start_hoy,
            end_hoy=end_hoy,
            earse_conditioned=earse_conditioned,
            mode=mode,
            preheat=preheat,
        )

        step = int(timestep)
        if step <= 0:
            raise ValueError("timestep must be a positive integer.")
        st_hoy = int(start_hoy)
        ed_hoy = int(end_hoy)
        if st_hoy < 0 or st_hoy > 8759:
            raise ValueError("start_hoy must be in [0, 8759].")
        if ed_hoy < 0 or ed_hoy > 8759:
            raise ValueError("end_hoy must be in [0, 8759].")
        if st_hoy > ed_hoy:
            raise ValueError("start_hoy must be <= end_hoy.")
        hoys = list(range(st_hoy, ed_hoy + 1, step))
        period_end_exclusive = ed_hoy + 1
        n = len(hoys)
        zone_by_name, zone_by_user = self._zone_lookup_maps()
        zone_names = [zn for zn in c_res.keys() if zn in e_res]
        result = {}
        for zn in zone_names:
            ez = e_res[zn]
            cz = c_res[zn]
            zone_dict = zone_by_name.get(zn)
            if zone_dict is None:
                zone_dict = zone_by_user.get(str(zn))
            heating_s, cooling_s, lighting_s = [], [], []
            total_energy_s, total_energy_vent_s = [], []
            comfort_s = list(cz.get("Comfort", []))[:n]
            ach_s = list(cz.get("ACH", []))[:n]
            temp_s = list(cz.get("Temperature", []))[:n]
            delta_s = list(cz.get("delta_t", []))[:n]
            mech_s = list(cz.get("MechanicalVent", []))[:n]
            if len(comfort_s) < n:
                comfort_s += [0] * (n - len(comfort_s))
            if len(ach_s) < n:
                ach_s += [np.nan] * (n - len(ach_s))
            if len(temp_s) < n:
                temp_s += [np.nan] * (n - len(temp_s))
            if len(delta_s) < n:
                delta_s += [np.nan] * (n - len(delta_s))
            if len(mech_s) < n:
                mech_s += [0.0] * (n - len(mech_s))
            comfort_hvac, occupancy, hvac_available = self._build_hvac_availability(
                zone_dict, hoys, comfort_s)
            startup_load, startup_event = self._startup_cooling_schedule(
                zone_dict, hoys, hvac_available, temp_s)
            total_base_load_s = []
            for i, hoy in enumerate(hoys):
                st = int(hoy)
                ed = min(8760, period_end_exclusive, st + step)
                h = self._window_mean(ez.get("heating_hourly", []), st, ed)
                c = self._window_mean(ez.get("cooling_hourly", []), st, ed)
                l = self._window_mean(ez.get("Lighting_hourly", []), st, ed)
                total_energy = h + c + l
                total_base_load = (h + c) if hvac_available[i] else 0.0
                total_energy_vent = total_base_load + float(startup_load[i]) + l + float(mech_s[i])
                heating_s.append(h)
                cooling_s.append(c)
                lighting_s.append(l)
                total_energy_s.append(total_energy)
                total_base_load_s.append(total_base_load)
                total_energy_vent_s.append(total_energy_vent)
            result[zn] = {
                "hoy": list(hoys),
                "zone_area": float(cz.get("zone_area", ez.get("zone_area", 0.0))),
                "heating": heating_s,
                "cooling": cooling_s,
                "lighting": lighting_s,
                "total_energy": total_energy_s,
                "total_base_load": total_base_load_s,
                "total_energy_vent": total_energy_vent_s,
                "startup_cooling_load": [float(v) for v in startup_load],
                "startup_cooling_event": [bool(v) for v in startup_event],
                "mechanical_vent_energy": mech_s,
                "Temperature": temp_s,
                "ACH": ach_s,
                "Comfort": comfort_s,
                "delta_t": delta_s,
                "HVACavailabilityFromComfort": comfort_hvac,
                "people_occupancy": occupancy,
                "HVACavailability": hvac_available,
            }
        return result

    def _zone_lookup_maps(self):
        by_name, by_user = {}, {}
        for key, zone in self.networkDict.get("zones", {}).items():
            by_user[str(zone.get("userName", key))] = zone
            by_name[str(zone.get("zone_name", zone.get("userName", key)))] = zone
        return by_name, by_user

    def _resolve_zone_series_value(self, zone_dict, hoy, field_name, default=0.0):
        if not isinstance(zone_dict, dict):
            return float(default)
        user = str(zone_dict.get("userName", zone_dict.get("zone_name", "zone")))
        try:
            return float(self._resolve_gain_value(zone_dict.get(field_name), hoy, field_name, user))
        except (ValueError, KeyError, TypeError):
            return float(default)

    def _build_hvac_availability(self, zone_dict, hoys, comfort):
        comfort_signal, occupancy, availability = [], [], []
        for i, hoy in enumerate(hoys):
            hvac_on = (float(comfort[i]) if i < len(comfort) else 0.0) < 0.5
            occupied = self._resolve_zone_series_value(zone_dict, hoy, "zone_ppsm") > 0.0
            comfort_signal.append(bool(hvac_on))
            occupancy.append(bool(occupied))
            availability.append(bool(hvac_on and occupied))
        return comfort_signal, occupancy, availability

    def _startup_cooling_schedule(self, zone_dict, hoys, availability, temperatures):
        loads, events = [], []
        was_on = False
        for i, hoy in enumerate(hoys):
            now_on = bool(availability[i]) if i < len(availability) else False
            startup = now_on and not was_on
            temp = float(temperatures[i]) if i < len(temperatures) else np.nan
            setpoint = self._resolve_zone_series_value(zone_dict, hoy, "zone_c_temp", 26.0)
            loads.append(0.0458 * max(temp - setpoint, 0.0) if startup and np.isfinite(temp) else 0.0)
            events.append(bool(startup))
            was_on = now_on
        return loads, events

    def revise(self, energyDict=None, model=None):
        """Apply network settings to the in-memory model without saving files.

        Explicit construction U-values and SHGC values remain authoritative.
        """
        network = self.networkDict if energyDict is None else energyDict
        model = self.model if model is None else model
        if not isinstance(network, dict) or model is None:
            raise ValueError("revise requires a network dictionary and model")
        spaces = {str(s.id): s for s in model.spaceList}
        updated = 0
        skipped = []
        protected = {"userName", "temperature", "prjIndex", "prjName", "heatLoad", "volume",
                     "position_x", "position_y", "position_z", "boundary", "level", "levelIndex",
                     "relHt", "contam", "balance", "element", "idf_template"}
        for key, values in network.get("zones", {}).items():
            name = str(values.get("userName", key))
            space = spaces.get(name)
            if space is None:
                skipped.append(f"zone {name}: no matching model space")
                continue
            for field, value in values.items():
                if field in protected or field not in space.settings or isinstance(value, (dict, list, tuple)):
                    continue
                if field in {"zone_wallU", "zone_winU", "zone_win_SHGC"}:
                    skipped.append(f"zone {name}: protected construction field {field}")
                    continue
                space.settings[field] = value
                updated += 1
        glazing_by_id = {}
        for glazing in getattr(model, "glazingList", []):
            glazing_by_id[str(glazing.Uid)] = glazing
            face_ids = getattr(glazing, "firstFaceId", None)
            if face_ids is not None:
                for face_id in np.asarray(face_ids, dtype=object).reshape(-1):
                    glazing_by_id[str(face_id)] = glazing
        stats = {"spaces_updated": updated, "glazings_updated": 0,
                 "glazings_resized": 0, "warnings": skipped}
        zone_by_prj = {}
        for key, value in network.get("zones", {}).items():
            try:
                zone_by_prj[int(value["prjIndex"])] = spaces.get(str(value.get("userName", key)))
            except (KeyError, TypeError, ValueError):
                continue
        for path_key, value in network.get("paths", {}).items():
            if not isinstance(value, dict):
                continue
            element = value.get("element") if isinstance(value.get("element"), dict) else {}
            try:
                window_type = int(round(float(value.get("winType", 1))))
            except (TypeError, ValueError):
                window_type = 1
            if (str(value.get("pathType", "")).lower() in {"leakage", "skylight"}
                    or str(element.get("dtype", "")).lower() == "plr_leak3"
                    or window_type != 1):
                continue
            glazing = next((glazing_by_id.get(str(k)) for k in
                            (value.get("Uid"), value.get("userName"), path_key)
                            if glazing_by_id.get(str(k)) is not None), None)
            if glazing is None:
                stats["warnings"].append(f"glazing path {path_key}: no matching target geometry")
                continue
            try:
                if element.get("operable") not in (None, ""):
                    glazing.operable = float(element["operable"])
                shgc = value.get("SHGC", element.get("SHGC"))
                if shgc is None:
                    for endpoint in (value.get("fromZone"), value.get("toZone")):
                        try:
                            adjacent = zone_by_prj.get(int(endpoint))
                        except (TypeError, ValueError):
                            adjacent = None
                        if adjacent is not None:
                            shgc = adjacent.settings.get("zone_win_SHGC")
                            if shgc is not None:
                                break
                explicit = {str(x).lower() for x in getattr(glazing, "explicit_settings", set())}
                if shgc is not None and not ({"shgc", "zone_win_shgc"} & explicit):
                    glazing.SHGC = float(shgc)
                    value["SHGC"] = glazing.SHGC
                    element["SHGC"] = glazing.SHGC
                    value["element"] = element
            except (TypeError, ValueError):
                stats["warnings"].append(f"glazing path {path_key}: invalid operability/SHGC")
            try:
                width, height = float(value["pathWidth"]), float(value["pathHeight"])
                parent = getattr(glazing, "parentFace", None)
                if width <= 0 or height <= 0 or parent is None:
                    raise ValueError("missing positive opening size or parent wall")
                faces = [f for f in np.asarray(getattr(parent, "face", []), dtype=object).reshape(-1)
                         if shapely.get_dimensions(f) == 2]
                center = shapely.points(glazing.getWeightCenter())
                selected = None
                for face in faces:
                    projection = Projection.fromPolygon(face)
                    uv = shapely.force_2d(projection.toUV(face))
                    center_uv = shapely.force_2d(projection.toUV(center))
                    if shapely.covers(shapely.buffer(uv, 1e-7), center_uv):
                        selected = (face, projection, uv, center_uv)
                        break
                if selected is None:
                    raise ValueError("opening center is outside parent wall")
                _, projection, wall_uv, center_uv = selected
                min_u, min_v, max_u, max_v = shapely.bounds(wall_uv)
                width, height = min(width, max_u - min_u), min(height, max_v - min_v)
                coord = shapely.get_coordinates(center_uv)[0]
                left = min(max(float(coord[0]) - width / 2, min_u), max_u - width)
                bottom = min(max(float(coord[1]) - height / 2, min_v), max_v - height)
                polygon = shapely.polygons([[left, bottom], [left + width, bottom],
                                            [left + width, bottom + height], [left, bottom + height],
                                            [left, bottom]])
                if not shapely.covers(shapely.buffer(wall_uv, 1e-7), polygon):
                    raise ValueError("requested opening does not fit parent wall")
                new_face = projection.toWorld(polygon)
                geo_id = model.includeGeo(new_face, normal=glazing.normal, cat=0)
                glazing.replaceGeo(geo_id)
                center_new = glazing.getWeightCenter()
                value.update({"pathWidth": width, "pathHeight": height,
                              "position_x": float(center_new[0]), "position_y": float(center_new[1]),
                              "position_z": float(center_new[2]), "Uid": str(glazing.Uid)})
                stats["glazings_resized"] += 1
            except (TypeError, ValueError, AttributeError) as exc:
                stats["warnings"].append(f"glazing path {path_key}: resize skipped ({exc})")
            stats["glazings_updated"] += 1
        self.model = model
        self.networkDict = network
        self.runtime["revise"] = stats
        return model

    def buildBalanceModel(self, networkDict=None, parameters=None):
        """Build the airflow-layer 5R1C model from this Coupler's snapshot."""
        network = self.networkDict if networkDict is None else networkDict
        options = parameters
        if options is None:
            options = network.get("thermal", {}).get("parameters")
        return BalanceModel.from_model(
            self.model, network, parameters=options, gain_resolver=self._resolve_gain_value)

    @staticmethod
    def _moving_average(values: np.ndarray, sigma: float) -> np.ndarray:
        """
        Gaussian weighted moving average for 1D signal.
        Each point is computed as a weighted sum of neighboring points,
        where weights follow a Gaussian distribution centered at the point.

        Parameters
        ----------
        values : np.ndarray
            Input 1D numeric array to be smoothed.
        sigma : float
            Standard deviation of the Gaussian kernel.
            Larger values produce stronger smoothing.

        Returns
        -------
        np.ndarray
            Smoothed array with the same shape as input.

        Notes
        -----
        - The effective window radius is set to 3 * sigma (covers 99.7% of Gaussian mass).
        - Edge handling: truncates window at array boundaries.
        - Fully vectorized implementation for high performance on large arrays.
        """
        values = np.asarray(values, dtype=np.float64)
        n = len(values)
        output = np.zeros_like(values)
        
        # Radius of the Gaussian window (3蟽 rule)
        radius = int(np.ceil(3.0 * sigma))
        if radius < 1:
            return values.copy()

        # Precompute indices for vectorized window operations
        indices = np.arange(n)
        for i in range(n):
            # Window start and end indices
            start = max(0, i - radius)
            end = min(n, i + radius + 1)
            window_indices = indices[start:end]
            
            # Gaussian weights calculation
            dist_sq = (window_indices - i) ** 2
            weights = np.exp(-dist_sq / (2.0 * sigma ** 2))
            weights /= weights.sum()  # Normalize to sum to 1
            
            # Compute weighted average
            output[i] = np.sum(values[window_indices] * weights)

        return output

    def _ensure_runtime_workspace(self, reset=False):
        """
        Prepare isolated per-task workspace and explicit vent runtime paths.

        Parameters
        ----------
        reset : bool, default False
            Whether to always create a new random workspace.
        """
        if (not reset) and self.runtime.get('workspace'):
            ws = self.runtime['workspace']
            if os.path.exists(ws.get('root', '')):
                return ws

        with SimulationWorkspace(prefix="moosas-vent-task-", retain=True) as workspace:
            root = str(workspace.path)
        project_dir = os.path.join(root, "project")
        result_dir = os.path.join(root, "result")
        room_info_file = os.path.join(root, "roomInfo.txt")
        os.makedirs(project_dir, exist_ok=True)
        os.makedirs(result_dir, exist_ok=True)

        ws = {
            'root': root,
            'project_dir': project_dir,
            'result_dir': result_dir,
            'room_info_file': room_info_file,
        }
        self.runtime['workspace'] = ws
        self.runtime['vent_paths'] = VentPaths.from_workspace(root)
        self.runtime['prj_counter'] = 0
        return ws

    def ventilationTask(self, hoy, energyDict: dict = None, iteration=1, inf_p=0.1, mode="onions"):
        """
        Run one-hour ventilation coupling through AirflowRunner.

        Parameters
        ----------
        hoy : int
            Hour-of-year index.
        energyDict : dict, optional
            Network dictionary override.
        iteration : int, default 1
            Maximum outer airflow/thermal iterations per hour; also caps 5R1C coupling.
        Returns
        -------
        tuple[AirflowZoneResult, ...]
            One-hour immutable zone results.
        """
        self._ensure_runtime_workspace(reset=False)
        if energyDict:
            self.networkDict = energyDict
        self.updateHeatLoad(hoy, energyDict=energyDict)
        if mode not in {"onions", "sequence", "ping-pong"}:
            raise ValueError("mode must be 'onions', 'sequence', or 'ping-pong'")
        thermal_settings = self.networkDict.get("thermal", {})
        if str(thermal_settings.get("backend", "legacy")).lower() == "5r1c":
            result = self._ventilation_task_5r1c(hoy, iteration, inf_p, thermal_settings)
            self.runtime["last_airflow_result"] = result
            self.runtime["last_prj_file"] = os.path.join(result.workspace.path, "project", "model.prj")
            return result.zones
        heat_loads = {
            str(zone.get("userName", zone_key)): float(zone["heatLoad"])
            for zone_key, zone in self.networkDict["zones"].items()
        }
        runner_options = {
            "model": self.model,
            "outdoor_temperature": self.runtime.get("outdoor_temperature", 25),
            "heat_loads": heat_loads,
            "max_iterations": 1 if mode == "sequence" else max(int(iteration), 1),
            "flow_multiplier": 1.0 + float(inf_p),
            "paths": self.runtime["vent_paths"],
        }
        if ("paths" in self.networkDict and all(
                "prjIndex" in zone for zone in self.networkDict.get("zones", {}).values())):
            runner_options["network_dict"] = self.networkDict
        result = AirflowRunner(**runner_options).run()
        self.runtime["last_airflow_result"] = result
        self.runtime["last_prj_file"] = os.path.join(
            result.workspace.path, "project", "model.prj"
        )
        return result.zones

    def _record_coupling_timing(self, settings, kind, row):
        """Append one durable CSV row; distinguish repeated HOYs with call_id."""
        folder = (settings.get('coupling') or {}).get('timing_dir')
        folder = folder or self.runtime.get('coupling_default_timing_dir') or os.path.join(self.runtime['vent_paths'].workspace, 'coupling_timing')
        os.makedirs(folder, exist_ok=True)
        filename = os.path.join(folder, kind + '.csv')
        exists = os.path.exists(filename) and os.path.getsize(filename) > 0
        with open(filename, 'a', newline='', encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(row))
            if not exists:
                writer.writeheader()
            writer.writerow(row)
        self.runtime['coupling_timing_dir'] = str(folder)

    def _ventilation_task_5r1c(self, hoy, iteration, inf_p, settings):
        """Transactional solve with timing, bounded rounds and explicit fallback."""
        base_paths = self.runtime['vent_paths']
        self.runtime['coupling_default_timing_dir'] = os.path.join(base_paths.workspace, 'coupling_timing')
        saved = {key: self.runtime[key] for key in (
            'balance_model', 'balance_state', 'balance_network_id', 'balance_parameters', 'balance_signature', 'coupling_last_diagnostics') if key in self.runtime}
        trial_paths = VentPaths.create(work_dir=base_paths.workspace)
        self.runtime['vent_paths'] = trial_paths
        self.runtime['coupling_call_id'] = uuid.uuid4().hex
        self.runtime['coupling_last_iteration'] = 0
        started = time.perf_counter()
        try:
            result = self._solve_5r1c_hour(hoy, iteration, inf_p, settings)
            self._record_coupling_timing(settings, 'hours', {
                'call_id': self.runtime['coupling_call_id'], 'phase': self.runtime.get('coupling_phase', 'independent'),
                'hoy': int(hoy), 'iterations': result.iteration_count,
                'elapsed_seconds': time.perf_counter() - started,
                'status': 'converged' if result.converged else 'tail_mean', 'error': ''})
            return result
        except Exception as exc:
            for key in ('balance_model', 'balance_state', 'balance_network_id', 'balance_parameters', 'balance_signature', 'coupling_last_diagnostics'):
                self.runtime.pop(key, None)
            self.runtime.update(saved)
            self.runtime['balance_failed_workspace'] = trial_paths.workspace
            self._record_coupling_timing(settings, 'hours', {
                'call_id': self.runtime['coupling_call_id'], 'phase': self.runtime.get('coupling_phase', 'independent'),
                'hoy': int(hoy), 'iterations': self.runtime.get('coupling_last_iteration', 0),
                'elapsed_seconds': time.perf_counter() - started, 'status': 'failed', 'error': str(exc)})
            raise
        finally:
            self.runtime['vent_paths'] = base_paths

    def _solve_5r1c_hour(self, hoy, iteration, inf_p, settings):
        """Trial solve; the caller isolates files from the committed project."""
        balance = self.runtime.get("balance_model")
        signature = BalanceModel.input_signature(self.model, self.networkDict, settings.get('parameters'))
        if (balance is None or self.runtime.get("balance_network_id") != id(self.networkDict)
                or self.runtime.get('balance_signature') != signature):
            balance = self.buildBalanceModel(parameters=settings.get("parameters"))
            self.runtime["balance_model"] = balance
            self.runtime["balance_state"] = balance.initial_state(settings.get("initial_temperature_c"))
            self.runtime["balance_network_id"] = id(self.networkDict)
            self.runtime["balance_parameters"] = deepcopy(settings.get("parameters") or {})
            self.runtime['balance_signature'] = signature
        committed = self.runtime.get("balance_state") or balance.initial_state()
        old_t = np.asarray(committed.ta_c, dtype=float)
        trial = old_t.copy()
        cp = balance.parameters.air_specific_heat_j_kgk
        coupling = {**settings, **(settings.get("coupling") or {})}
        requested = int(iteration)
        configured = int(coupling.get('max_iterations', afn.COUPLING_MAX_ITERATIONS))
        max_coupling = min(requested, configured)
        t_tol = float(coupling.get("temperature_tolerance_c", afn.TEMPERATURE_TOLERANCE_C))
        f_tol = float(coupling.get("flow_tolerance", afn.FLOW_TOLERANCE))
        relax = float(coupling.get("relaxation", afn.RELAXATION))
        policy = coupling.get('on_nonconvergence', 'tail_mean')
        tail_count = int(coupling.get('tail_iterations', 3))
        budget = coupling.get('max_hour_seconds')
        budget = None if budget is None else float(budget)
        if (max_coupling < 1 or t_tol <= 0 or f_tol <= 0 or not 0 < relax <= 1
                or tail_count < 1 or policy not in {'tail_mean', 'error'}
                or (budget is not None and (not np.isfinite(budget) or budget <= 0))):
            raise ValueError("Invalid 5R1C airflow coupling configuration")
        started = time.perf_counter()
        tail = deque(maxlen=tail_count)
        self.runtime['coupling_last_iteration'] = 0
        solar = {
            str(zone.get('userName', key)): float(zone.get('zone_transmitted_solar_w', 0.0))
            for key, zone in self.networkDict.get('zones', {}).items()
        }
        # An explicit backend mapping remains a per-zone override, rather than
        # an additional term, so SHGC is never applied twice.
        solar.update(settings.get("solar_w") or {})
        gains = balance.gains(hoy, internal=bool(settings.get('internal_gains', True)), solar_w=solar)
        previous_flow = None
        flow_residual = float("inf")
        candidate = committed
        airflow_result = None
        exit_reason = 'iteration_limit'
        converged = False
        for coupling_iteration in range(1, max_coupling + 1):
            if budget is not None and time.perf_counter() - started >= budget:
                exit_reason = 'time_budget'
                break
            self.runtime['coupling_last_iteration'] = coupling_iteration
            tick = time.perf_counter()
            contam_seconds = thermal_seconds = 0.0
            inner_iterations = None
            error = ''
            t_residual = float('inf')
            try:
                indoor = dict(zip(balance.zone_ids, trial))
                runner_kwargs = {}
                if budget is not None:
                    runner_kwargs['timeout_seconds'] = min(300.0, max(0.001, budget - (tick - started)))
                afn_start = time.perf_counter()
                try:
                    airflow_result = AirflowRunner(
                        model=self.model, outdoor_temperature=self.runtime.get("outdoor_temperature", 25),
                        heat_loads={z: 0.0 for z in balance.zone_ids}, max_iterations=1,
                        flow_multiplier=1.0, paths=self.runtime["vent_paths"], thermal=False,
                        indoor_temperature=indoor, network_dict=self.networkDict, **runner_kwargs).run()
                finally:
                    contam_seconds = time.perf_counter() - afn_start
                if airflow_result.mass_flow_matrix_kg_s is None:
                    raise RuntimeError("CONTAM did not return its native kg/s airflow matrix")
                source_names = tuple(z.user_name for z in airflow_result.zones)
                source_matrix = np.asarray(airflow_result.mass_flow_matrix_kg_s, dtype=float)
                full_matrix = np.zeros((len(balance.zone_ids) + 1, len(balance.zone_ids) + 1), dtype=float)
                target_nodes = [balance.index[name] for name in source_names] + [len(balance.zone_ids)]
                full_matrix[np.ix_(target_nodes, target_nodes)] = source_matrix
                boundary = AirflowThermalBoundary.from_mass_matrix(balance.zone_ids, full_matrix, cp_j_kgk=cp)
                thermal_start = time.perf_counter()
                try:
                    candidate, inner_iterations = balance.step(
                        committed, hoy, self.runtime.get("outdoor_temperature", 25), gains, boundary,
                        ideal_hvac=bool(settings.get("ideal_hvac", True)))
                finally:
                    thermal_seconds = time.perf_counter() - thermal_start
                candidate_t = np.asarray(candidate.ta_c, dtype=float)
                if not np.isfinite([candidate.ta_c, candidate.ts_c, candidate.tm_c]).all():
                    raise ValueError('5R1C produced non-finite candidate state')
                t_residual = float(np.max(np.abs(candidate_t - trial)))
                if previous_flow is not None:
                    denom = max(float(np.linalg.norm(previous_flow)), 1e-12)
                    flow_residual = float(np.linalg.norm(full_matrix - previous_flow) / denom)
                    converged = t_residual <= t_tol and flow_residual <= f_tol
                tail.append((candidate, full_matrix.copy()))
            except Exception as exc:
                error = str(exc)
                raise
            finally:
                self._record_coupling_timing(settings, 'iterations', {
                    'call_id': self.runtime['coupling_call_id'], 'phase': self.runtime.get('coupling_phase', 'independent'),
                    'hoy': int(hoy), 'iteration': coupling_iteration, 'iteration_limit': max_coupling,
                    'elapsed_seconds': time.perf_counter() - tick, 'contam_seconds': contam_seconds,
                    'thermal_seconds': thermal_seconds, 'thermal_internal_iterations': inner_iterations,
                    'temperature_residual_c': t_residual if np.isfinite(t_residual) else None,
                    'relative_flow_residual': flow_residual if np.isfinite(flow_residual) else None,
                    'status': 'failed' if error else ('converged' if converged else 'candidate'), 'error': error})
            if converged:
                exit_reason = 'converged'
                break
            previous_flow = full_matrix.copy()
            trial = (1.0 - relax) * trial + relax * candidate_t
        if not converged:
            if policy == 'error' or not tail:
                raise RuntimeError(
                    f"CONTAM-5R1C did not converge at hoy={hoy} after {self.runtime['coupling_last_iteration']} iterations "
                    f"(temperature={t_residual:.6g} C, flow={flow_residual:.6g}, reason={exit_reason})")
            # Every candidate took one step from the same committed state.
            # Averaging all three nodes prevents a mixed, inconsistent hour state.
            candidate = BalanceState(balance.zone_ids, *(
                tuple(np.mean([getattr(item[0], field) for item in tail], axis=0))
                for field in ('ta_c', 'ts_c', 'tm_c')))
            full_matrix = np.mean([item[1] for item in tail], axis=0)
        candidate_t = np.asarray(candidate.ta_c, dtype=float)
        existing = {z.user_name: z for z in airflow_result.zones}
        diagnostics = {'hoy': int(hoy), 'converged': converged, 'exit_reason': exit_reason,
                       'iterations': self.runtime['coupling_last_iteration'], 'iteration_limit': max_coupling,
                       'tail_samples': 0 if converged else len(tail), 'policy': policy,
                       'temperature_residual_c': t_residual,
                       'relative_flow_residual': flow_residual if np.isfinite(flow_residual) else None}
        project_file = os.path.join(airflow_result.workspace.path, "project", "model.prj")
        _write_project_temperatures(
            np.asarray([candidate_t[balance.index[name]] + 273.15 for name in source_names]), project_file)
        with open(os.path.join(airflow_result.workspace.path, 'coupling_diagnostics.json'), 'w', encoding='utf-8') as handle:
            json.dump(diagnostics, handle, indent=2, allow_nan=False)
        zone_out = tuple(
            replace(existing[name], temperatures=(float(candidate_t[i]),),
                    ach_values=(float(full_matrix[-1, i] * afn.TIME_STEP_SECONDS
                                      / balance.parameters.air_density_kg_m3 / balance.volume[i]),))
            if name in existing else AirflowZoneResult(
                user_name=name, project_name="", heat_load=0.0, volume=float(balance.volume[i]),
                temperatures=(float(candidate_t[i]),), ach_values=(0.0,))
            for i, name in enumerate(balance.zone_ids))
        warning = () if converged else (
            f'CONTAM-5R1C not converged; tail_mean of {len(tail)} candidates; raw path_results/project flow files refer to final CONTAM trial.',)
        # Files and structured result must be built successfully before committing.
        result = replace(airflow_result, zones=zone_out, converged=converged,
                         iteration_count=self.runtime['coupling_last_iteration'], residual=max(t_residual, flow_residual),
                         mass_flow_matrix_kg_s=full_matrix.copy(),
                         airflow_matrix=full_matrix * afn.TIME_STEP_SECONDS / balance.parameters.air_density_kg_m3,
                         warnings=airflow_result.warnings + warning)
        self.runtime['balance_state'] = candidate
        self.runtime['coupling_last_diagnostics'] = diagnostics
        return result

    @staticmethod
    def _is_variable_flow_path_dict(path_obj: dict, eps=1e-9) -> bool:
        try:
            h = float(path_obj.get("pathHeight", 0.0))
            w = float(path_obj.get("pathWidth", 0.0))
            return (h > eps) and (w > eps)
        except Exception:
            return False

    @staticmethod
    def _is_leakage_path_dict(path_obj: dict) -> bool:
        path_type = str(path_obj.get("pathType", "")).strip().lower()
        dtype = str((path_obj.get("element") or {}).get("dtype", "")).strip().lower()
        return path_type == "leakage" or dtype == "plr_leak3"

    def _remove_non_ambient_connected_from_dict(self, energyDict: dict) -> dict:
        zones = dict(energyDict.get("zones", {}))
        paths = dict(energyDict.get("paths", {}))
        if len(zones) == 0:
            energyDict["zones"] = zones
            energyDict["paths"] = {}
            return energyDict

        zone_prj_set = {str(z.get("prjIndex")) for z in zones.values()}
        topology = {"-1": set()}
        for zone_prj in zone_prj_set:
            topology[zone_prj] = set()

        for p in paths.values():
            from_zone = str(p.get("fromZone", ""))
            to_zone = str(p.get("toZone", ""))
            from_ok = (from_zone == "-1") or (from_zone in zone_prj_set)
            to_ok = (to_zone == "-1") or (to_zone in zone_prj_set)
            if (not from_ok) or (not to_ok):
                continue
            if not self._is_variable_flow_path_dict(p):
                continue
            topology.setdefault(from_zone, set()).add(to_zone)
            topology.setdefault(to_zone, set()).add(from_zone)

        visited = {"-1"}
        queue = ["-1"]
        while len(queue) > 0:
            current = queue.pop(0)
            for nxt in topology.get(current, set()):
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append(nxt)
        keep_zone_prj = {p for p in visited if p != "-1"}

        zones = {
            zone_key: z
            for zone_key, z in zones.items()
            if str(z.get("prjIndex")) in keep_zone_prj
        }
        valid_prj = {str(z.get("prjIndex")) for z in zones.values()}
        filtered_paths = {}
        for path_key, p in paths.items():
            from_zone = str(p.get("fromZone", ""))
            to_zone = str(p.get("toZone", ""))
            from_ok = (from_zone == "-1") or (from_zone in valid_prj)
            to_ok = (to_zone == "-1") or (to_zone in valid_prj)
            if from_ok and to_ok:
                filtered_paths[path_key] = p

        energyDict["zones"] = zones
        energyDict["paths"] = filtered_paths
        return energyDict

    def _build_erase_conditioned_energy_dict(self, baseEnergyDict: dict, discomfort_zone_users: set[str]) -> dict:
        filtered = deepcopy(baseEnergyDict)
        zones = dict(filtered.get("zones", {}))
        paths = dict(filtered.get("paths", {}))

        remove_zone_prj = set()
        kept_zones = {}
        for zone_key, z in zones.items():
            zone_user = str(z.get("userName", zone_key))
            if zone_user in discomfort_zone_users:
                remove_zone_prj.add(str(z.get("prjIndex")))
                continue
            kept_zones[zone_key] = z
        zones = kept_zones

        valid_prj = {str(z.get("prjIndex")) for z in zones.values()}
        kept_paths = {}
        for path_key, p in paths.items():
            if self._is_leakage_path_dict(p):
                continue
            from_zone = str(p.get("fromZone", ""))
            to_zone = str(p.get("toZone", ""))
            if (from_zone in remove_zone_prj) or (to_zone in remove_zone_prj):
                continue
            from_ok = (from_zone == "-1") or (from_zone in valid_prj)
            to_ok = (to_zone == "-1") or (to_zone in valid_prj)
            if from_ok and to_ok:
                kept_paths[path_key] = p

        filtered["zones"] = zones
        filtered["paths"] = kept_paths
        return self._remove_non_ambient_connected_from_dict(filtered)

    def annualComfort(self, energyDict: dict = None, iteration=3, timestep=1,
                      k=0.5, sigma=1, inf_p=0.1, start_hoy=0, end_hoy=8759,
                      earse_conditioned=False, mode="ping-pong", preheat=afn.PREHEAT_ROUNDS, **kwargs):
        """
        Run annual ventilation comfort simulation with selectable coupling strategy.

        Parameters
        ----------
        energyDict : dict, optional
            Network dictionary override.
        iteration : int, default 3
            Maximum outer airflow/thermal iterations per hour; also caps 5R1C coupling.
        timestep : int, default 1
            Hour step size. Number of hoy samples is
            `len(range(start_hoy, end_hoy + 1, timestep))`.
        k : float, default 0.5
            Thermal inertia strength. Recommended range [0, 1].
        sigma : float, default 1
            Gaussian smoothing sigma for moving-average outdoor temperature on sampled sequence.
        inf_p : float, default 0.1
            Infiltration fraction of outdoor air for each zone. Recommended range [0, 1].
        start_hoy : int, default 0
            Inclusive start hour-of-year for comfort simulation.
        end_hoy : int, default 8759 
            Inclusive end hour-of-year for comfort simulation.
        Returns
        -------
        dict
            {"zone_name": {"zone_area": ..., "ACH": [...], "Temperature": [...]} }
        """
        self._ensure_runtime_workspace(reset=True)
        if energyDict:
            self.networkDict = energyDict
        if int(timestep) <= 0:
            raise ValueError("timestep must be a positive integer.")
        if mode not in {"onions", "sequence", "ping-pong"}:
            raise ValueError("mode must be 'onions', 'sequence', or 'ping-pong'")
        if int(preheat) < 0:
            raise ValueError("preheat must be non-negative")
        legacy_w = kwargs.pop("w", None)
        if kwargs:
            raise TypeError(f"Unexpected keyword arguments: {', '.join(kwargs.keys())}")
        if legacy_w is not None:
            sigma = legacy_w
        if float(sigma) < 0:
            raise ValueError("sigma must be non-negative.")
        st_hoy = int(start_hoy)
        ed_hoy = int(end_hoy)
        if st_hoy < 0 or st_hoy > 8759:
            raise ValueError("start_hoy must be in [0, 8759].")
        if ed_hoy < 0 or ed_hoy > 8759:
            raise ValueError("end_hoy must be in [0, 8759].")
        if st_hoy > ed_hoy:
            raise ValueError("start_hoy must be <= end_hoy.")
        hoys = list(range(st_hoy, ed_hoy + 1, int(timestep)))
        raw_weather_t = self.weather.temperature.tolist()
        if len(raw_weather_t) >= 8761:
            try:
                float(raw_weather_t[0])
            except (TypeError, ValueError):
                raw_weather_t = raw_weather_t[1:]
        if len(raw_weather_t) < 8760:
            raise ValueError("Weather temperature series must contain at least 8760 hourly values.")
        full_weather_t = [float(raw_weather_t[h]) for h in range(8760)]
        full_avg_t = self._moving_average(full_weather_t, sigma=float(sigma))
        weather_t = [full_weather_t[h] for h in hoys]
        avg_t = [full_avg_t[h] for h in hoys]
        indoor_ref_t = [(1.0 - float(k)) * t + float(k) * a for t, a in zip(weather_t, avg_t)]
        outdoor_series = weather_t

        base_network_for_hours = deepcopy(self.networkDict)
        thermal_backend = str(self.networkDict.get("thermal", {}).get("backend", "legacy")).lower()
        if thermal_backend == "5r1c":
            # A new annual run starts from its configured initial state.
            for key in ('balance_model', 'balance_state', 'balance_network_id', 'balance_signature'):
                self.runtime.pop(key, None)
            if earse_conditioned:
                raise ValueError("earse_conditioned is not supported with the stateful 5R1C backend")
            if int(timestep) != 1:
                raise ValueError("5R1C yearly coupling requires consecutive hourly steps (timestep=1)")
        zone_users = [str(z.get("userName", k)) for k, z in base_network_for_hours.get("zones", {}).items()]
        user_to_zone_name = {
            str(z.get("userName", k)): str(z.get("zone_name", z.get("userName", k)))
            for k, z in base_network_for_hours.get("zones", {}).items()
        }
        user_to_zone_area = {
            str(z.get("userName", k)): float(z.get("zone_area", 0.0))
            for k, z in base_network_for_hours.get("zones", {}).items()
        }
        temp_hourly_by_user = {zu: [] for zu in zone_users}
        ach_hourly_by_user = {zu: [] for zu in zone_users}
        mech_hourly_by_user = {zu: [] for zu in zone_users}

        def _extract_hour_result(zones_result):
            out_temp = {zu: np.nan for zu in zone_users}
            out_ach = {zu: np.nan for zu in zone_users}
            if not zones_result:
                return out_temp, out_ach
            for zr in zones_result:
                zone_user = zr.user_name
                if zone_user not in out_temp:
                    continue
                if zr.temperatures:
                    out_temp[zone_user] = zr.temperatures[-1]
                if zr.ach_values:
                    out_ach[zone_user] = zr.ach_values[-1]
            return out_temp, out_ach

        preheat_hoy = None
        if thermal_backend == "5r1c" and int(preheat) > 0:
            settings = self.networkDict.get('thermal', {})
            balance = self.buildBalanceModel(parameters=settings.get('parameters'))
            def heat_at(hour):
                self.updateHeatLoad(hour, energyDict=self.networkDict)
                gains = balance.gains(hour, internal=bool(settings.get('internal_gains', True)),
                                      solar_w={
                                          str(zone.get('userName', key)): float(zone.get('zone_transmitted_solar_w', 0.0))
                                          for key, zone in self.networkDict.get('zones', {}).items()
                                      })
                return sum(g['internal_w'] + g['solar_w'] for g in gains.values())
            # Match source: maximum gains within the requested interval;
            # ties select its first hour, not the coldest hour of the full year.
            preheat_hoy = max(hoys, key=heat_at)
            self.runtime["outdoor_temperature"] = full_weather_t[preheat_hoy]
            self.runtime['coupling_phase'] = 'preheat'
            for _ in range(int(preheat)):
                self.ventilationTask(preheat_hoy, energyDict=self.networkDict,
                                     iteration=iteration, inf_p=inf_p, mode=mode)
        if thermal_backend == '5r1c':
            self.runtime['balance_preheat'] = {'hoy': preheat_hoy, 'rounds': int(preheat)}

        self.runtime['coupling_phase'] = 'hourly'
        for hi, hoy in enumerate(hoys):
            print("--------------------Hoy:", hoy)
            self.runtime['outdoor_temperature'] = outdoor_series[hi]
            hour_temp = {zu: np.nan for zu in zone_users}
            hour_ach = {zu: np.nan for zu in zone_users}
            zResultHoy = self.ventilationTask(
                hoy=hoy,
                energyDict=energyDict,
                iteration=iteration,
                inf_p=inf_p,
                mode=mode
            )
            if (not zResultHoy) or not zResultHoy[0].temperatures or not zResultHoy[0].ach_values:
                raise RuntimeError(f"Airflow simulation returned no result at hoy={hoy}")
            else:
                hour_temp, hour_ach = _extract_hour_result(zResultHoy)

            comfort_now = {}
            out_t = float(weather_t[hi])
            ref_t = float(indoor_ref_t[hi])
            discomfort_users = set()
            for zu in zone_users:
                t_raw = float(hour_temp[zu]) if np.isfinite(hour_temp[zu]) else np.nan
                if not np.isfinite(t_raw):
                    comfort_now[zu] = 0
                    discomfort_users.add(zu)
                    continue
                t_eval = t_raw if thermal_backend == "5r1c" else (t_raw - 25.0) + ref_t
                is_comfort = (0.31 * out_t <= t_eval < 0.31 * out_t + 20.3)
                comfort_now[zu] = 1 if is_comfort else 0
                if not is_comfort:
                    discomfort_users.add(zu)

            if bool(earse_conditioned) and (len(discomfort_users) > 0):
                filtered_energy = self._build_erase_conditioned_energy_dict(
                    baseEnergyDict=base_network_for_hours,
                    discomfort_zone_users=discomfort_users
                )
                zResultSecond = self.ventilationTask(
                    hoy=hoy,
                    energyDict=filtered_energy,
                    iteration=iteration,
                    inf_p = inf_p
                )
                second_temp, second_ach = _extract_hour_result(zResultSecond)
                kept_users = {
                    str(z.get("userName", k))
                    for k, z in filtered_energy.get("zones", {}).items()
                }
                for zu in kept_users:
                    if zu in hour_temp and np.isfinite(second_temp.get(zu, np.nan)):
                        hour_temp[zu] = float(second_temp[zu])
                    if zu in hour_ach and np.isfinite(second_ach.get(zu, np.nan)):
                        hour_ach[zu] = float(second_ach[zu])

            mech_this_h = self._mechanical_energy_kwh_m2_by_zone(
                prj_file=self.runtime.get('last_prj_file'),
                hoy=hoy,
                specific_power=0.27
            )
            for zu in zone_users:
                temp_hourly_by_user[zu].append(float(hour_temp[zu]) if np.isfinite(hour_temp[zu]) else np.nan)
                ach_hourly_by_user[zu].append(float(hour_ach[zu]) if np.isfinite(hour_ach[zu]) else np.nan)
                mech_hourly_by_user[zu].append(float(mech_this_h.get(zu, 0.0)))

        result = {}
        for zu in zone_users:
            zone_name = user_to_zone_name.get(zu, zu)
            zone_area = float(user_to_zone_area.get(zu, 0.0))
            raw_temp = _linear_interpolate_nan_series(temp_hourly_by_user.get(zu, []))
            raw_ach = _linear_interpolate_nan_series(ach_hourly_by_user.get(zu, []))
            if len(raw_temp) < len(hoys):
                raw_temp += [np.nan] * (len(hoys) - len(raw_temp))
            if len(raw_ach) < len(hoys):
                raw_ach += [np.nan] * (len(hoys) - len(raw_ach))

            if thermal_backend == "5r1c":
                delta_zt = list(raw_temp)
                z_t = list(raw_temp)
            else:
                delta_zt = [float(t) - 25.0 if np.isfinite(t) else np.nan for t in raw_temp]
                z_t = [(float(dz) + float(ref_t)) if np.isfinite(dz) else np.nan
                       for dz, ref_t in zip(delta_zt, indoor_ref_t)]
            comfort = []
            for out_t, t_val in zip(weather_t, z_t):
                if not np.isfinite(t_val):
                    comfort.append(0)
                else:
                    comfort.append(1 if (0.31 * out_t <= t_val < 0.31 * out_t + 20.3) else 0)

            mech = list(mech_hourly_by_user.get(zu, []))
            if len(mech) < len(hoys):
                mech += [0.0] * (len(hoys) - len(mech))
            result[zone_name] = {
                "zone_area": zone_area,
                "hoys": int(8760.0 / len(hoys)) if len(hoys) > 0 else 0,
                "ACH": [float(v) if np.isfinite(v) else np.nan for v in raw_ach],
                "delta_t": [float(v) if np.isfinite(v) else np.nan for v in delta_zt],
                "Temperature": [float(v) if np.isfinite(v) else np.nan for v in z_t],
                "outdoorTemp": [float(v) for v in weather_t],
                "baseIndoorTemp": [float(v) for v in indoor_ref_t],
                "Comfort": comfort,
                "MechanicalVent": [float(v) for v in mech[:len(hoys)]],
            }
        return result


