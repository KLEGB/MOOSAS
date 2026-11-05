"""NBuilding annual on/off latent-load and RDF integration check.

python -m test.run_cetus194_nbuilding --output test/output/cetus194
"""
import argparse,contextlib,csv,json,time
from pathlib import Path
import numpy as np
from MoosasPy.model import MoosasModel
from MoosasPy.simulation.weather import load_epw
from MoosasPy.simulation.energy import build_energy_input,EnergyRunner


def main():
    p=argparse.ArgumentParser(__doc__);p.add_argument('--output',type=Path,default=Path('test/output/cetus194'));a=p.parse_args()
    root=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=True)
    with (out/'annual_model.log').open('w') as f,contextlib.redirect_stdout(f):
        m=MoosasModel.load(root/'test/caseFile/NBuilding.rdf')
        w=load_epw(root/'MoosasPy/db/CHN_BJ_Beijing-Nanyuan.AP.545120_TMYx.epw',out/'annual_weather')
    records={};runs={}
    for flag in (False,True):
        name='on' if flag else 'off';start=time.monotonic()
        r=build_energy_input(m,w,latent={'enabled':flag})
        print(name,len(r.zones),'zones',flush=True)
        result=EnergyRunner(energy_input=r,on_progress=lambda n,total: print(f'{name} {n}/{total}',flush=True) if n%744==0 or n==total else None).run()
        records[name]=result
        runs[name]=dict(seconds=time.monotonic()-start,max_thermal_balance_residual_w=result.max_balance_residual_w,
            warmup_cycles=len(result.warmup),latent_load=result.latent_load,
            electricity_kwh=float(result.zone_hourly_energy_kwh.sum()),warnings=result.warnings)
    off,on=records['off'],records['on']
    np.testing.assert_array_equal(off.zone_hourly_energy_kwh,on.zone_hourly_energy_kwh)
    np.testing.assert_array_equal(off.zone_temperature_c,on.zone_temperature_c)
    assert off.final_state==on.final_state
    assert on.latent_load['latent_cooling_kwh']>0
    assert on.max_moisture_residual_kg_s<1e-10
    with (out/'NBuilding_hourly.csv').open('w',newline='') as f:
        writer=csv.writer(f);writer.writerow(['zone_id','hoy','zone_c','humidity_ratio_kg_kg','rh_fraction','sensible_cooling_w','latent_cooling_w','total_cooling_w','dehumidification_kg_s'])
        for i,z in enumerate(on.request.zones):
            for h in range(on.request.hours):
                writer.writerow([z.zone_id,h,on.zone_temperature_c[i,h],on.zone_humidity_ratio[i,h],on.zone_relative_humidity[i,h],on.zone_sensible_cooling_w[i,h],on.zone_latent_cooling_w[i,h],on.zone_sensible_cooling_w[i,h]+on.zone_latent_cooling_w[i,h],on.zone_dehumidification_kg_s[i,h]])
    np.savez_compressed(out/'NBuilding_results.npz',temperature_c=on.zone_temperature_c,
        sensible_electricity_kwh=on.zone_hourly_energy_kwh,latent_cooling_w=on.zone_latent_cooling_w,
        sensible_cooling_w=on.zone_sensible_cooling_w,humidity_ratio=on.zone_humidity_ratio,
        relative_humidity=on.zone_relative_humidity,dehumidification_kg_s=on.zone_dehumidification_kg_s)
    report=dict(base_commit='eab365642545ad0d3b9da9f7f69d2d3beac2ef07',model='NBuilding.rdf',
        zones=len(on.request.zones),hours=on.request.hours,zone_hours=len(on.request.zones)*on.request.hours,
        total_area_m2=sum(z.area_m2 for z in on.request.zones),runs=runs,
        max_temperature_difference_k=float(np.max(abs(off.zone_temperature_c-on.zone_temperature_c))),
        max_existing_energy_difference_kwh=float(np.max(abs(off.zone_hourly_energy_kwh-on.zone_hourly_energy_kwh))),
        final_thermal_state_exact=off.final_state==on.final_state,
        humidity_ratio_min=float(w.humidity_ratio.min()),humidity_ratio_max=float(w.humidity_ratio.max()),
        nonfinite_indoor_humidity_count=int((~np.isfinite(on.zone_relative_humidity)).sum()),
        weather='Bundled Beijing-Nanyuan TMY; software test, not NBuilding site calibration',
        radiation='Existing unshaded weather projection; no new Radiance run')
    (out/'NBuilding_validation.json').write_text(json.dumps(report,indent=2,allow_nan=False))
    print(json.dumps(report,indent=2),flush=True)


if __name__=='__main__':main()
