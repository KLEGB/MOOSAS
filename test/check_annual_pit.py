"""Compare an existing annual result with weather-matched native PIT samples.

python -m test.check_annual_pit /path/to/moosas-annual-<id>
"""
import argparse
import json
from pathlib import Path
import shutil

import numpy as np
from MoosasPy.simulation.radiation import RadianceRunner


def main():
    parser=argparse.ArgumentParser(__doc__)
    parser.add_argument('run_dir')
    parser.add_argument('--sensors',type=int,default=32)
    args=parser.parse_args()
    source=Path(args.run_dir).resolve()
    manifest=json.loads((source/'manifest.json').read_text())
    location=manifest['location']
    root=source/'pit-check';root.mkdir(exist_ok=True)
    shutil.copyfile(source/'scene.rad',root/'scene.rad')
    annual=np.load(source/'illuminance_total.npy',mmap_mode='r')
    indices=np.linspace(0,annual.shape[0]-1,min(args.sensors,annual.shape[0]),dtype=int)
    lines=(source/'sensors.pts').read_text().splitlines()
    (root/'points.pts').write_text('\n'.join(lines[i] for i in indices)+'\n')
    weather=np.loadtxt(source/'weather.wea',skiprows=6)
    runner=RadianceRunner(None,timeout_seconds=600)
    hemisphere='''
skyfunc glow sky_glow
0
0
4 1 1 1 0
sky_glow source sky
0
0
4 0 0 1 180
skyfunc glow ground_glow
0
0
4 1 1 1 0
ground_glow source ground
0
0
4 0 0 -1 180
'''
    records=[]
    for month,day,hour in [(1,21,12.5),(3,21,12.5),(6,21,12.5),(9,21,15.5)]:
        time=int(np.where((weather[:,0]==month)&(weather[:,1]==day)&(weather[:,2]==hour))[0][0])
        dni,dhi=weather[time,3:5]
        sky=runner._run_command((str(runner._executable('gendaylit')),str(month),str(day),str(hour),
            '-a',str(location['latitude']),'-o',str(-location['longitude_east']),'-m',str(-15*location['utc_offset_hours']),
            '-W',str(dni),str(dhi),'-O','0'),root)
        if manifest['request']['north_rotation_deg'] != 0:
            raise ValueError('This PIT verification script currently requires zero north rotation')
        (root/'sky.rad').write_text(sky.stdout+hemisphere)
        with (root/'model.oct').open('wb') as out:
            runner._run_command((str(runner._executable('oconv')),'scene.rad','sky.rad'),root,stdout=out)
        with (root/'points.pts').open() as inp:
            trace=runner._run_command((str(runner._executable('rtrace')),'-h','-I+','-ab','5','-ad','8192',
                '-dc','1','-dt','0','-dj','0','-aa','0','-lw','0.00001','model.oct'),root,stdin=inp)
        reference=np.array(runner._parse_illuminances(trace.stdout))
        predicted=annual[indices,time]
        records.append(dict(month=month,day=day,hour=hour,dni=float(dni),dhi=float(dhi),
            reference_mean=float(reference.mean()),annual_mean=float(predicted.mean()),
            nmae=float(np.mean(abs(reference-predicted))/max(reference.mean(),1)),
            mean_bias=float((predicted.mean()-reference.mean())/max(reference.mean(),1))))
    result=dict(sensor_indices=indices.tolist(),samples=records)
    (root/'comparison.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    main()
