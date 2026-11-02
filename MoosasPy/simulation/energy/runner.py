"""Run independent surface-resolved zones through the persistent CES3 v2 engine."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import platform

import numpy as np

from ..contracts import SimulationResult
from ..engine import SubprocessEngine
from ..runner import CommandResult
from .input import build_energy_input
from .protocol import BinarySurfaceEngineV2
from .requests import EnergyRequest, EnergyState

PARTS = ('cooling','heating','lighting','equipment')
MONTH_NAMES = ('Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec')
MONTH_START = np.array((0,31,59,90,120,151,181,212,243,273,304,334,365))*24


def _row(values):
    result = dict(zip(PARTS, map(float, values)))
    result['total'] = sum(result.values())
    return result


@dataclass(frozen=True)
class EnergyZoneResult:
    zone_id: str
    name: str
    area_m2: float
    energy_kwh: tuple[float, ...]

    @property
    def load(self):
        return _row(np.asarray(self.energy_kwh)/self.area_m2)

    @property
    def params(self):
        return dict(zone_name=self.name, zone_area=self.area_m2)


@dataclass(frozen=True)
class EnergyResult(SimulationResult):
    request: EnergyRequest | None = None
    zone_results: tuple[EnergyZoneResult, ...] = ()
    hour_of_year: tuple[int, ...] = ()
    zone_hourly_energy_kwh: np.ndarray | None = None
    zone_temperature_c: np.ndarray | None = None
    final_state: EnergyState | None = None
    max_balance_residual_w: float = 0.
    warmup: tuple[float, ...] = ()
    surface_hours: tuple = ()
    temporal_scale: str = 'monthly'
    spatial_scale: str = 'building'

    @property
    def data(self):
        """Derived unrounded kWh/m² view for existing analysis consumers."""
        areas = np.array([z.area_m2 for z in self.zone_results])
        absolute = self.zone_hourly_energy_kwh
        intensity = absolute / areas[:,None,None]
        building = absolute.sum(axis=0) / areas.sum()
        data = dict(total=_row(building.sum(axis=0)),spaces=self.zone_results)
        hoy = np.asarray(self.hour_of_year)
        if self.temporal_scale == 'monthly':
            months = [building[(hoy>=a)&(hoy<b)].sum(axis=0) for a,b in zip(MONTH_START[:-1],MONTH_START[1:])]
            data['months'] = dict(zip(MONTH_NAMES,map(_row,months)))
            if self.spatial_scale=='zone':
                data['zone_months'] = tuple(tuple(_row(z[(hoy>=a)&(hoy<b)].sum(axis=0)) for a,b in zip(MONTH_START[:-1],MONTH_START[1:])) for z in intensity)
        elif self.temporal_scale=='daily':
            days = tuple(np.unique(hoy//24))
            data['days'] = tuple(_row(building[hoy//24==d].sum(axis=0)) for d in days)
            if self.spatial_scale=='zone':
                data['zone_days'] = tuple(tuple(_row(z[hoy//24==d].sum(axis=0)) for d in days) for z in intensity)
        else:
            data['hours'] = tuple(map(_row,building))
            if self.spatial_scale=='zone':
                data['zone_hours'] = tuple(tuple(map(_row,z)) for z in intensity)
        return data


class EnergyRunner:
    def __init__(self, model=None, weather=None, *, energy_input=None,
                 executable_path=None, engine=None, timeout_seconds=120.,
                 temporal_scale='monthly', spatial_scale='building',
                 preheat=None, max_preheat=120, warmup_tolerance_k=.1,
                 initial_state=None, surface_results=False, on_progress=None, on_hour=None, **input_options):
        if temporal_scale not in {'monthly','daily','hourly'}:
            raise ValueError('temporal_scale must be monthly, daily, or hourly')
        if spatial_scale not in {'building','zone'}:
            raise ValueError('spatial_scale must be building or zone')
        if energy_input is not None and not isinstance(energy_input,EnergyRequest):
            raise TypeError('energy_input must be EnergyRequest; CSV/CLI zone dictionaries are no longer supported')
        self.request = energy_input if energy_input is not None else build_energy_input(model,weather,**input_options)
        suffix = '.exe' if platform.system()=='Windows' else ''
        packaged = Path(__file__).resolve().parents[2]/'libs'/'energy'/f'CetusSurfaceEnergy{suffix}'
        self.executable = Path(executable_path or os.environ.get('MOOSAS_ENERGY_EXECUTABLE',packaged)).resolve()
        if not self.executable.is_file():
            raise FileNotFoundError(f'CETUS CES3 v2 executable missing: {self.executable}')
        if os.name!='nt' and not os.access(self.executable,os.X_OK):
            self.executable.chmod(self.executable.stat().st_mode | 0o111)
        self.engine = engine or SubprocessEngine()
        if not np.isfinite(timeout_seconds) or timeout_seconds<=0:
            raise ValueError('timeout_seconds must be positive')
        self.timeout_seconds = timeout_seconds
        self.temporal_scale = temporal_scale
        self.spatial_scale = spatial_scale
        self.preheat = (0 if initial_state is not None else 4) if preheat is None else preheat
        if int(self.preheat)!=self.preheat or self.preheat<0 or int(max_preheat)!=max_preheat or max_preheat<self.preheat or warmup_tolerance_k<=0:
            raise ValueError('Invalid warmup controls')
        if self.preheat and (initial_state is not None or self.request.start_hour+24>8760):
            raise ValueError('Restart/end-of-year windows require preheat=0')
        self.max_preheat = max_preheat
        self.tolerance = warmup_tolerance_k
        self.initial_state = initial_state
        self.surface_results = surface_results
        self.on_progress = on_progress
        self.on_hour = on_hour

    def run(self):
        request = self.request
        signature = request.signature
        zone_ids = tuple(z.zone_id for z in request.zones)
        if self.initial_state is not None:
            state = self.initial_state
            if state.model_signature!=signature or state.zone_ids!=zone_ids or state.next_hour!=request.start_hour:
                raise ValueError('Restart signature, zone ordering, or next hour differs from request')
            states = state.states
        else:
            states = tuple(() for _ in request.zones)
        models = tuple(z.native_model() for z in request.zones)
        energy = np.zeros((len(models),request.hours,4))
        temperatures = np.zeros((len(models),request.hours))
        maximum_residual = 0.
        history, surfaces = [], []
        warnings = list(request.warnings)
        session = BinarySurfaceEngineV2(self.executable,self.timeout_seconds,self.engine.open_binary_session)
        with session:
            session.configure(models)
            def step(hour, full=False):
                nonlocal states,maximum_residual
                session.begin_step(request.hour(hour),states)
                response = session.evaluate(full=full)
                states = response['states']
                maximum_residual = max(maximum_residual,response['max_abs_residual_w'])
                session.end_step()
                return response['zones']
            if self.preheat:
                for cycle in range(self.max_preheat):
                    before = states
                    for hour in range(24):
                        step(hour)
                    change = max((max(abs(a-b) for a,b in zip(old,new)) if old else float('inf')) for old,new in zip(before,states))
                    history.append(change)
                    if cycle+1>=self.preheat and change<=self.tolerance:
                        break
                else:
                    warnings.append(f'Warmup did not converge to {self.tolerance} K in {self.max_preheat} daily cycles')
            for hour in range(request.hours):
                rows = step(hour,self.surface_results or self.on_hour is not None)
                for i,(zone,row) in enumerate(zip(request.zones,rows)):
                    q = row['hvac_w']
                    h = request.start_hour+hour
                    energy[i,hour] = (max(-q,0)/zone.cooling_cop/1000,
                                      max(q,0)/zone.heating_cop/1000,
                                      zone.lighting_w[h]/1000,zone.equipment_w[h]/1000)
                    temperatures[i,hour] = row['zone_c']
                if self.surface_results:
                    surfaces.append(tuple(row['surfaces'] for row in rows))
                if self.on_hour is not None:
                    self.on_hour(request.start_hour+hour,rows)
                if self.on_progress and ((hour+1)%24==0 or hour+1==request.hours):
                    self.on_progress(hour+1,request.hours)
        energy.setflags(write=False)
        temperatures.setflags(write=False)
        results = tuple(EnergyZoneResult(z.zone_id,z.name,z.area_m2,tuple(energy[i].sum(axis=0))) for i,z in enumerate(request.zones))
        checkpoint = EnergyState(signature,request.start_hour+request.hours,zone_ids,states)
        command = CommandResult(session.command,session.process.returncode,'CES3 v2 numeric session',session.stderr)
        return EnergyResult(commands=(command,),warnings=tuple(warnings),request=request,zone_results=results,
            hour_of_year=tuple(range(request.start_hour,request.start_hour+request.hours)),zone_hourly_energy_kwh=energy,
            zone_temperature_c=temperatures,final_state=checkpoint,max_balance_residual_w=maximum_residual,
            warmup=tuple(history),surface_hours=tuple(surfaces),temporal_scale=self.temporal_scale,spatial_scale=self.spatial_scale)
