"""Reproducible NBuilding TTL, Energy/AFN input, and annual Radiance exercise.

python -m test.run_annual_nbuilding NBuilding.rdf results --grid-spacing 0.5
Uses the bundled Beijing TMY for a software test, not site certification.
"""
import argparse
import contextlib
import json
from pathlib import Path
import time

import numpy as np
from MoosasPy.model import MoosasModel
from MoosasPy.simulation.airflow.network import getZoneAndPath
from MoosasPy.simulation.energy.input import build_energy_input
from MoosasPy.simulation.weather import prepare_daylight_weather, load_epw
from MoosasPy.simulation.radiation import RadianceRunner, AnnualDaylightRequest


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('model')
    parser.add_argument('output')
    parser.add_argument('--grid-spacing', type=float, default=.5)
    parser.add_argument('--divisions', type=int, default=4096)
    parser.add_argument('--processes', type=int, default=2)
    parser.add_argument('--probe-window', default=None)
    parser.add_argument('--probe-wall', default=None)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    with (output / 'model.log').open('w') as log, contextlib.redirect_stdout(log):
        model = MoosasModel.load(args.model)
        # Exercise settings on every object while leaving optical values absent:
        # annual output must disclose defaults for this legacy input.
        collections = (model.faceList, model.wallList, model.glazingList, model.skylightList, model.shadingList)
        elements = {str(e.Uid):e for group in collections for e in group}
        for i, element in enumerate(elements.values()):
            element.settings['persistence_probe'] = {'index':i, 'flags':[True, False], 'value':.123}
        for space in model.spaceList:
            space.settings['daylight_occupied_mask'] = [8 <= h % 24 < 18 for h in range(8760)]
        glazing = next((w for w in model.glazingList if str(w.Uid)==args.probe_window), None) if args.probe_window else min(model.glazingList,key=lambda w:str(w.Uid))
        if glazing is None:
            raise ValueError('Unknown probe-window UID')
        glazing.settings.update(visible_transmittance=.61, solar_transmittance=.42, operable=.37,
                               airflow_element={'cd':.61})
        wall = next((w for w in model.wallList if str(w.Uid)==args.probe_wall), None) if args.probe_wall else min(model.wallList,key=lambda w:str(w.Uid))
        if wall is None:
            raise ValueError('Unknown probe-wall UID')
        wall.settings.update(visible_reflectance=.52, solar_reflectance=.31)
        model.save(output / 'NBuilding_roundtrip.ttl')
        restored = MoosasModel.load(output / 'NBuilding_roundtrip.ttl')
    after = {str(e.Uid):e for group in (restored.faceList, restored.wallList, restored.glazingList, restored.skylightList, restored.shadingList) for e in group}
    for uid, e in elements.items():
        assert after[uid].settings['persistence_probe'] == e.settings['persistence_probe'], uid
    for s in restored.spaceList:
        assert s.settings['daylight_occupied_mask'] == [8 <= h % 24 < 18 for h in range(8760)]
    zones, paths = getZoneAndPath(restored, calculate_heat=False)
    path = next(p for p in paths if p.userName == str(glazing.Uid))
    assert path.element['operable'] == .37 and path.element['cd'] == .61
    epw = Path(__file__).resolve().parents[1] / 'MoosasPy/db/CHN_BJ_Beijing-Nanyuan.AP.545120_TMYx.epw'
    weather = load_epw(str(epw), str(output / 'weather'))
    energy = build_energy_input(restored, weather, hours=24)
    matched = [surface for zone in energy.zones for surface in zone.surfaces if surface.source_element_id == str(glazing.Uid)]
    assert matched and all(surface.transmittance == .42 for surface in matched)
    matched_walls = [surface for zone in energy.zones for surface in zone.surfaces if surface.source_element_id == str(wall.Uid)]
    assert matched_walls and all(surface.reflectance == .31 for surface in matched_walls)
    prepared = time.monotonic()
    wea = prepare_daylight_weather(str(epw), str(output / 'weather.wea'))
    result = RadianceRunner(restored, timeout_seconds=3600).run_annual(AnnualDaylightRequest(
        wea, output_dir=str(output), grid_spacing_m=args.grid_spacing,
        ambient_divisions=args.divisions, processes=args.processes))
    total = np.load(result.illuminance_total_file, mmap_mode='r')
    direct = np.load(result.illuminance_direct_file, mmap_mode='r')
    assert total.shape == direct.shape == (result.sensor_count, 8760)
    assert np.isfinite(total).all() and (total >= 0).all()
    assert np.isfinite(direct).all() and (direct >= 0).all()
    assert np.all(total >= direct - 1e-3)
    report = dict(input_file=str(Path(args.model).resolve()),spaces=len(restored.spaceList), elements_checked=len(elements),
        airflow_zones=len(zones),airflow_paths=len(paths), energy_zones=len(energy.zones),
        test_window=str(glazing.Uid),test_wall=str(wall.Uid),input_solar_transmittance=.42,input_vlt=.61,input_operable=.37,input_discharge_coefficient=.61,
        sensor_count=result.sensor_count, shape=list(total.shape),manifest=result.manifest_file,
        preparation_seconds=prepared-start,annual_seconds=time.monotonic()-prepared,
        total_max_lux=float(total.max()),direct_max_lux=float(direct.max()),warnings=result.warnings)
    (output / 'validation.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2),flush=True)


if __name__ == '__main__':
    main()
