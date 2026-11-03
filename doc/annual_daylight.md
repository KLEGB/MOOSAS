# Annual Radiance daylight and model properties

Based on develop `9a4562dfc17162362950b765aed88f464b81ab30` (merged 1.3.5).
The implementation reuses `RadianceRunner`; PIT and annual logic remain in
`simulation/radiation/runner.py`. New model entities and a separate annual
solver package are unnecessary. The only new public data types are the
annual request/result. Weather produces a plain WEA file.

## Use

```python
from MoosasPy.model import MoosasModel
from MoosasPy.simulation.weather import prepare_daylight_weather
from MoosasPy.simulation.radiation import RadianceRunner, AnnualDaylightRequest
import numpy as np

model = MoosasModel.load("NBuilding.ttl")
wea = prepare_daylight_weather("Beijing.epw", "results/weather.wea")
result = RadianceRunner(model, timeout_seconds=3600).run_annual(
    AnnualDaylightRequest(wea, output_dir="results", processes=2)
)
illuminance = np.load(result.illuminance_total_file, mmap_mode="r")
```

A unique retained run directory is returned. Its `manifest.json` is written
only after all solver stages and validations succeed. Partial failed run
files remain available for diagnostics and do not have a completed manifest.
Large matrices never pass through command stdout strings or model TTL.

## Method and limits

Fixed-state ordinary glass and opaque diffuse surfaces are supported.
Radiance computes RGB daylight coefficients and a full visible Perez sky,
subtracts the coarse direct-sun contribution, and adds exact sun contributions:
`E = Cfull Sfull - Cdirect Ssun + Eexact`. The black scene used for exact
sun retains glass transmission and all occluding geometry; opaque reflectance
is zero. Exact sun uses `rcontrib -I+ -V+ -ab 0`; source strength is included
exactly once. Sun modifier minute numbers map explicitly back to all 8760
time columns. Ground is sky-matrix row zero. North rotation applies only to
the sky/suns.

The supported material properties are achromatic scalars. An internal
coefficient scene therefore encodes full transport in red/blue and direct
transport in green, with opaque green reflectance set to zero. Glass
transmission remains identical in all channels. A **single** `rfluxmtx` run
accumulates both paths using exactly the same samples. Direct terms cancel
without the independent Monte Carlo noise of separate full/black coefficient
runs. An analytic uniform-sky/ground + 50%-reflecting-wall regression checks
full irradiance against `.75*pi` and direct irradiance against `.5*pi`.

The artificial transport channels are not physical color bands. Because all
supported optics are neutral, the actual Perez RGB sky may first be converted
to photopic values using `179*(.265R+.670G+.065B)`. The numerically stable
combination is `Cfull*(Sfull-Ssun) + (Cfull-Cdirect)*Ssun + Eexact`. Coefficients
must satisfy `Cfull >= Cdirect`. This also preserves the physically necessary
`total illuminance >= direct illuminance` without clipping negative sampling
artifacts. Colored/BSDF optical models would need independent transport-band
handling and are not accepted as scalar visible properties. NumPy combines
Radiance transport matrices; it introduces no additional light engine.

Defaults: MF=2 (578 rows including ground), 4096 ambient divisions, 5 bounces,
0.5 m clipped grid and 0.76 m horizontal workplane. Every disconnected floor
piece and floor hole is preserved. Each sensor stores its actual clipped
representative area and room/floor identity. Fixed shading is included.
Sloped workplanes fail explicitly. Fully glazed opaque parents export no
opaque surface. Failed nonempty geometry exports fail the run rather than
silently creating light leaks.

sDA is area-weighted after per-point DA classification. DA/cDA thresholds
are configurable (default 300 lx, 50% time). UDI is `<100`, `100..2000`,
`>2000` lx by default. ASE uses only exact direct-sun illuminance (`>1000 lx`
for `>250 h`); thresholds are configurable. All ratios are stored as 0..1.
These results use the model's fixed shading state and explicit evaluation
mask. They **are not full LM-83 operational-shading compliance assessments**.
No automatic blind movement, BSDF, daylight dimming or lighting-energy model
is introduced. Transport coefficients are not automatically reused across
runs; this avoids stale geometry/material cache reuse in the first version.

Each space may supply `settings['daylight_occupied_mask']`, 8760 booleans.
A request's `occupied_by_space` takes precedence. Otherwise the existing
`zone_work_start/end` define a daily local-standard-time interval on every
day, with a warning; no weekday or holiday calendar is guessed. Nighttime
occupancy remains in denominators. Zero occupancy yields empty metrics and
`no_occupied_hours` status, rather than an apparent 0% score.

Weather validation rejects missing/negative DNI/DHI, malformed/duplicate
hours, subhourly and leap/incomplete years. EPW records are end-of-hour;
WEA uses interval midpoints. The EPW DATA PERIODS header must declare one
record per hour. Hourly EPWs with conventional 0/30/60 minute markers are
accepted; the interval, not the marker, controls midpoint conversion.
Fractional UTC offsets and full EPW coordinate precision are preserved.
There is no radiation-to-weather package import.

PIT keeps its existing call. `RadianceSky.utc_offset_hours` defaults to UTC;
set it explicitly for local standard time, or use a timezone-aware datetime.
PIT now preserves minutes and converts east-positive longitude to Radiance's
west-positive convention. PIT and annual share per-element material/scene
export. PIT's existing ideal `gensky` result should not be compared with an
actual-weather annual time column. To use `gendaylit` within the same PIT
runner, supply both `RadianceSky.direct_normal_irradiance_w_m2` and
`diffuse_horizontal_irradiance_w_m2`. The sky command is retained in diagnostics. Weather-PIT daylight factor is
`None`, since a daylight factor is not defined under the actual sunny sky.
Existing PIT sensor defaults are preserved; numerical cross-checks must supply
the same coordinates/workplane rather than assume PIT's old adaptive grid
matches the new clipped annual grid.

## One existing settings dictionary, three distinct physical domains

| Object | Existing canonical settings | Consumer |
|---|---|---|
| Opaque walls/slabs | `visible_reflectance` (0..1) | Radiance plastic material |
| Windows/skylights | `visible_transmittance` | Radiance glass, physical normal-incidence VLT converted to transmissivity |
| Walls/windows | `layers`, `u_value`, `u_convention`, `thermal_mass_j_m2k`, `volumetric_heat_capacity`, `thickness` | Energy surface construction |
| Walls/windows | `solar_reflectance`, `solar_transmittance`, `outside_emissivity`, `inside_emissivity`, film and view coefficients | Energy surface energy balance |
| Windows/skylights | `operable`, `airflow_element` | CONTAM opening fraction and existing native element dictionary (`cd`, `dtype`, etc.) |
| Spaces | Existing thermal templates, absolute schedules, setpoints, infiltration and internal gains | Energy / airflow heat-balance adapters |
| Spaces | `daylight_occupied_mask`, existing work-start/end fallback | Annual daylight metric mask |

Canonical properties are `moosas:hasSetting` keys plus the corresponding
`moosas:<key>` literal. Scalars retain native RDF types; compound values use
the existing typed JSON literal. Arrays, dictionaries, booleans, numeric
strings and NumPy arrays retain their types. No new RDF class is introduced.
Existing plain scalar literals and legacy element fields remain readable.
Independent shading settings and explicit-setting markers now round-trip too.
RDF space/element settings and their explicit markers are stored and read only
as per-subject triples. The energy metadata payload retains model-level settings,
schedules, conditioning and element ownership, but no duplicate settings snapshots.
XML/JSON retain their complete metadata snapshots because they use a different
persistence representation.

Settings present only in an old RDF metadata snapshot are no longer imported.
Export such files again from their source model before using this version.
The misspelled `zone_inflitration` key is rejected; use `zone_infiltration`.
Windows and skylights expose `SHGC` and `operable` through their settings so
direct edits reach both serialization and the airflow adapter.

`visible_transmittance != solar_transmittance != SHGC`. Ambiguous old
`transparency/reflection` fields are preserved but not automatically assigned
a visible spectral band. Missing visible values produce disclosed defaults:
VLT .70, floor reflectance .30, ceiling .75, other opaque surfaces .60.
Ordinary Radiance glass with n=1.52 supports VLT up to approximately .918;
higher values are rejected rather than generating nonphysical transmissivity.
VLT=0 becomes opaque black. Complex optics need a later explicit extension.

`optical_overrides[uid]` can override canonical visible properties without
altering the persisted model. Unknown element IDs are rejected. The manifest
records assumptions, resolved optics, native executable hashes, source hashes,
actual commands, grid settings, time convention and metric thresholds.

## Native tool selection

The default runtime is the bundled Windows/Linux x86-64 Radiance installation.
Other platforms must supply a complete installation through `executable_dir`.
Every executable is resolved only in the selected directory; missing tools fail
explicitly instead of using the old weather tools or a system PATH installation.
The runner's subprocess environment uses the selected `bin` first on PATH and
its adjacent `lib` plus the run directory on RAYPATH. It does not import legacy
or inherited Radiance function-library search paths.

Matrix inputs must declare `NROWS`, `NCOLS` and `NCOMP=3` and contain the expected
finite RGB data. ASCII, native float and native double payloads are supported.

## Reproduce NBuilding validation

From the repository root on this Windows workspace:

```powershell
$env:PYTHONPATH = (Get-Location).Path
$env:OPENBLAS_NUM_THREADS = '2'
& 'D:\miniconda3\Scripts\conda.exe' run -n moosas python test\run_annual_nbuilding.py test\caseFile\NBuilding.rdf results
& 'D:\miniconda3\Scripts\conda.exe' run -n moosas python -m pytest test\test_annual_daylight.py
```

The explicit source import path avoids a collision between this repository's
`test` directory and Python installations that provide a standard-library
`test` package.

The integration script uses the bundled Beijing TMY solely as a repeatable
software test. It deliberately sets different visible/solar transmission,
opening fraction and discharge coefficient on one window, round-trips every
element and every room through TTL, then checks Energy and airflow inputs
before running all rooms for 8760 hours. Optical defaults for the legacy
input remain disclosed. Results are not a measured validation or a certified
assessment of the building's as-built glazing and occupancy.

## Windows develop integration validation

The cleaned integration passed 574 tests with 7 skipped in the `moosas` Conda
environment on Windows. The NBuilding integration exercised all 37 spaces and
275 elements through TTL, Energy and CONTAM, then computed 710 sensors by 8760
hours with a 2 m grid, 1024 ambient divisions and two native processes. All
values were finite and satisfied total illuminance >= direct illuminance >= 0.
This is a coarse-grid integration check, not a repeat of the 0.5 m reference run.

Weather-matched PIT checks at 32 identical sensors gave normalized mean
absolute differences of 5.53%, 3.08%, 2.18% and 6.02% for the four seasonal
timestamps used by `check_annual_pit.py`. Native analytic-reflection and exact
direct-sun regressions also passed their 2% and 1% relative tolerances.

The wheel built successfully and all 25 annual-runtime files matched their
source hashes. On this machine E-drive file-handle `fstat` calls returned
`WinError 87`; using a C-drive wheel staging directory avoided that filesystem
problem without changing project packaging configuration.
