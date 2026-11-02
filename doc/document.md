# MoosasPy

MoosasPy is the Python core of MOOSAS, a building-performance analysis and
optimization toolkit for the early design stage. It converts geometry into a
structured building model and provides model exchange, weather preparation,
energy analysis, solar-radiation and daylight calculation, airflow simulation,
and coupled analysis workflows.

This repository contains the Python package and its native runtime resources.
It does not contain the former SketchUp plug-in, Ruby code, browser interface,
or a portable Python distribution.

## Package Structure

```text
MoosasPy/
|-- model/                 Building model and model-file I/O
|   `-- io/                RDF, XML, JSON, IDF, graph, gbXML, and IFC adapters
|-- transform/             Geometry-to-model transformation pipeline
|   |-- importers/         GEO, OBJ, and STL readers
|   |-- stages/            Explicit transformation stages and options
|   |-- geometry/          Geometry, topology, contours, and space generation
|   `-- alignment/         Ontology and geometry alignment helpers
|-- simulation/
|   |-- energy/            Rapid energy analysis and PV conversion
|   |-- radiation/         Radiation, sunlight, and Radiance daylight analysis
|   |-- airflow/           Airflow networks and CONTAM execution
|   |-- weather/           Weather records, EPW preparation, and sky models
|   `-- coupling/          Cross-domain workflows
|-- utils/                 Shared constants, paths, errors, and utilities
|-- db/                    Templates, schedules, weather, and EnergyPlus data
|-- libs/                  Packaged native executables and support files
|-- data/                  Legacy runtime examples
`-- __temp__/              Runtime workspace; not included in distributions
```

The three main public API areas are:

| API | Purpose |
| --- | --- |
| `MoosasPy.transform` | Convert GEO, OBJ, or STL geometry into a `MoosasModel`. |
| `MoosasPy.MoosasModel` | Load, inspect, edit, and save complete building models. |
| `MoosasPy.simulation` | Run energy, radiation, weather, airflow, and coupled workflows. |

## Requirements and Installation

MoosasPy requires Python 3.10 or newer. From the repository root, install the
package with:

```bash
python -m pip install .
```

For development, tests, and package validation, install the development extras:

```bash
python -m pip install -e ".[dev]"
```

Verify the installation:

```bash
python -c "import MoosasPy; print(MoosasPy.__version__)"
```

Package versions are derived from Git tags matching
`moosaspy-vMAJOR.MINOR.PATCH`.

## Usage

### Quick Start

Transform a geometry source and save the resulting model:

```python
from MoosasPy.transform import TransformOptions, transform

options = TransformOptions(attach_shading=True)
model = transform("building.geo", options=options, stdout=None)

print(len(model.spaceList))
model.save("building.ttl")
```

Load the saved model without rerunning geometry transformation:

```python
from MoosasPy import MoosasModel

model = MoosasModel.load("building.ttl")
model.summary()
```

At the package root, `transform` is the transformation module rather than the
function itself:

```python
from MoosasPy import transform

model = transform.transform("building.obj", stdout=None)
```

Importing the function directly from `MoosasPy.transform` is recommended for
application code.

### Geometry Transformation

Geometry transformation is the required entry point for raw geometry:

```python
transform(
    input_path: str,
    input_type: str | None = None,
    *,
    method=CCRSpaceGeneration,
    options: TransformOptions | None = None,
    stdout=sys.stdout,
) -> MoosasModel
```

Parameters:

- `input_path`: path to a GEO, OBJ, or STL source.
- `input_type`: optional explicit `"geo"`, `"obj"`, or `"stl"` override.
- `method`: space-generation callable. The default is `CCRSpaceGeneration`.
- `options`: immutable `TransformOptions` shared by all pipeline stages.
- `stdout`: transformation log stream. Pass `None` to suppress log output.

`transform()` returns a complete `MoosasModel`. It does not write an output
model automatically; call `model.save()` explicitly.

The current transformation options are:

| Option | Default | Effect |
| --- | ---: | --- |
| `solve_duplicated` | `True` | Remove duplicated geometry. |
| `solve_redundant` | `True` | Merge redundant coplanar geometry. |
| `solve_overlap` | `True` | Resolve overlapping faces. |
| `triangulate_faces` | `True` | Triangulate horizontal faces where required. |
| `break_wall_vertical` | `True` | Split walls at building levels. |
| `break_wall_horizontal` | `True` | Split walls at horizontal intersections. |
| `attach_shading` | `False` | Retain unused faces as shading or thermal mass. |
| `divided_zones` | `False` | Convexify complex zones. |
| `simplify_boundary` | `False` | Simplify source boundaries before generation. |
| `insert_core` | `False` | Generate offset perimeter/core thermal zones and AirBoundary partitions; automatically enables spatial division. No physical core is inserted. |
| `standardize` | `False` | Standardize the finalized model geometry. |

Example with explicit processing choices:

```python
from MoosasPy.transform import TransformOptions, transform

options = TransformOptions(
    attach_shading=True,
    divided_zones=True,
    standardize=True,
)
model = transform("massing.obj", options=options)
```

Do not use model-file formats such as RDF, XML, JSON, or IDF as transformation
inputs. Those formats enter through `MoosasModel.load()`.

Convex partitioning uses robust XY orientation predicates from GEOS, without
coordinate-dependent collinearity thresholds or geometric snapping. Segment
intersection includes endpoint contact, collinear overlap, and zero-length
segments. Partition diagonals must be strictly internal: polygon edges,
boundary overlaps, and diagonals passing through another boundary vertex are
not accepted. Concave-vertex detection uses the same orientation predicate.
The shortest-valid-diagonal strategy is unchanged; correcting these predicates
can change the resulting partition count.

Wall loops alone do not define enclosed spaces. Open-to-sky courtyards remain
holes in the surrounding building footprint, but are neither emitted as spaces
or voids nor convex-partitioned by `divided_zones`. Physical overhead coverage is
checked across higher storeys, so a roofed multi-storey atrium can still be
assembled from stacked voids. Opaque roofs and glazed skylights count as cover;
shading-only faces and air boundaries do not.

The default coordinate and recognition precision is **0.001 m (1 mm)**,
controlled by `geom.POINT_PRECISION`. GEO import retains its existing
coordinate-clustering and floor-quantization rules at this precision.
Wall-projection intersections, topology nodes, preprocessing height groups,
and IDF vertex output use the same precision. In particular, wall splitting
must not use a coarser intersection grid, which can shift shared junctions
and merge adjacent room contours. Area, minimum room dimensions, and angular
thresholds are unchanged. Rebuild models from their original
geometry sources to benefit from the finer precision: loading an existing
model or IDF cannot restore coordinates already discarded.

### Building Model

`MoosasModel` stores geometry, spaces, topology, thermal settings, schedules,
weather references, and resources required by downstream analyses. Common
collections include:

- `spaceList`: conditioned and unconditioned spatial units.
- `wallList`: vertical opaque faces.
- `faceList`: horizontal faces, including floors and ceilings.
- `glazingList`: window elements.
- `skylightList`: skylight elements.
- `levelList`: detected building levels.
- `buildingTemplate`: available thermal-setting templates.
- `schedule` and `scheduleByType`: loaded schedule definitions.

Use `model.summary()` for a level-by-level element and area summary.

### Model I/O

Complete model I/O is exposed through the model itself:

```python
from MoosasPy import MoosasModel

model = MoosasModel.load("building.rdf")
result = model.save("building.xml")

print(result.primary_path)
print(result.generated_paths)
```

Supported formats:

| Format | Load | Save | Notes |
| --- | :---: | :---: | --- |
| RDF / Turtle (`.rdf`, `.ttl`) | Yes | Yes | Turtle serialization is used. |
| XML (`.xml`) | Yes | Yes | Uses a same-named `.geo` geometry sidecar. |
| JSON (`.json`) | Yes | Yes | Uses a same-named `.geo` geometry sidecar. |
| EnergyPlus IDF (`.idf`) | Yes | Yes | Requires EnergyPlus 26.1 format. |
| Graph JSON (`.graph.json`) | No | Yes | Graph-oriented export. |
| gbXML (`.gbxml`) | No | Yes | Exported through the RDF-to-gbXML adapter. |

GEO, OBJ, and STL are geometry sources, not complete serialized models. Load
them with `transform()`.

IFC conversion and inspection functions are available from
`MoosasPy.model.io.ifc`, including `writeIfc`, `loadIfc`, `rdf_to_ifc`,
`ifc_to_rdf`, and `inspect_ifc`. IFC is intentionally not dispatched through
`MoosasModel.load()` or `model.save()` because its conversion contract is
different from the complete-model formats above.

#### EnergyPlus IDF Contract

MoosasPy reads and writes EnergyPlus 26.1 IDF files using the bundled 26.1
`Energy+.idd`. Older IDFs must first be migrated with the official EnergyPlus
Transition chain. MoosasPy does not provide an automatic legacy-IDF fallback.

Exported surface vertices use `GlobalGeometryRules.Vertex Entry Direction =
Counterclockwise`, viewed from outside the owning zone. Floors point downward;
ceilings and roofs point upward, preserving the slope of inclined faces. Walls
point out of the zone, windows follow their host surface, and paired interzone
surfaces have opposite normals. The geometry projection basis is right-handed;
rebuild models from source geometry rather than reusing caches made with the
previous projection convention.

Ground floors retain the mapped template's ground method, construction, and
matching site ground-temperature object, rather than using the generic wall
U-value. `Ground` uses `Site:GroundTemperature:BuildingSurface`;
`GroundFCfactorMethod` uses the template F-factor and FC-factor temperatures.
For each continuous slab, actual exposed perimeter (including courtyards, not
shared edges) is allocated to floor faces by area. Each construction's area
matches its face. If allocated perimeter exceeds the face's geometric perimeter,
it is capped and the local F-factor scaled inversely, preserving total F*P and
uniform equivalent resistance. This avoids negative resistance in tiny thermal
partitions. Disconnected slabs, elevations, and different template F-factors are
handled separately. Model vertices and wall segmentation are not changed.
Ground-floor seams within `geom.POINT_PRECISION` (0.001 m) are accepted:
overlap is tested on the original footprints after each is inset by half
that tolerance, not inferred from precision-grid union area differences.
Larger overlaps raise an error naming both surfaces and zones. Perimeter
allocation is normalized by the sum of the member face areas, preserving
total F*P despite seam and grid-overlay area differences.

If a mapped template zone has no ground floor, its ground method can be inherited
from other template zones only when unambiguous. Ambiguous methods,
missing/incompatible constructions, and unsupported Foundation/preprocessor
methods raise an export error. Missing ground temperatures produce a warning;
the adapter does not invent them or reinterpret FC-factor temperatures.

After export, construction and material definitions that are not reachable from
remaining IDF consumers are removed using IDD references. References from internal
mass, window shading, and control objects are included, not just surface layers.

Template-based export retains `DesignSpecification:OutdoorAir`'s calculation
method, independent per-person, per-area, per-zone and air-change rates, and
schedule. Zero-valued components do not overwrite other components, and `Sum`
or `Maximum` remain evaluated by EnergyPlus. `zone_pfav` records the per-person
component in m3/(h person), not an aggregation of these rates. Model-authored
templates can still set the per-person rate explicitly.

### Apply Thermal Settings

Each `MoosasSpace` receives a default residential template during model
construction. Apply another packaged or user-provided template with
`space.applySettings()`:

```python
for space in model.spaceList:
    space.applySettings("climatezone3_GB/T51350-2019_RESIDENTIAL")

model.spaceList[0].settings["zone_equipment"] = 8.8
```

The argument can be an exact template key, a regular-expression hint matching
a key in `model.buildingTemplate`, or a template dictionary already present in
that mapping. Applying a template also updates face U-values and glazing SHGC.

Important energy-setting fields include:

| Field | Meaning |
| --- | --- |
| `zone_wallU` | Opaque-envelope U-value in W/(m²·K). |
| `zone_winU` | Window U-value in W/(m²·K). |
| `zone_win_SHGC` | Window solar heat-gain coefficient. |
| `zone_c_temp`, `zone_h_temp` | Cooling and heating set points in °C. |
| `zone_collingEER`, `zone_HeatingEER` | Cooling and heating efficiency values. |
| `zone_ppsm` | Occupant density in people/m². |
| `zone_pfav` | Outdoor-air requirement per person. |
| `zone_popheat` | Sensible heat per person in W/person. |
| `zone_equipment`, `zone_lighting` | Equipment and lighting loads in W/m². |
| `zone_infiltration`, `zone_nightACH` | Infiltration and night ventilation rates. |
| `zone_template` | Applied template identifier. |

Building templates are stored in `MoosasPy/db/building_template.csv`; schedule
libraries are stored under `MoosasPy/db/schedule/`.

### Weather Data

Weather APIs use immutable `Location` and `WeatherData` records. Convert a
user-provided EPW file in a caller-owned output directory:

```python
from MoosasPy.simulation.weather import load_epw

weather = load_epw("custom.epw", "analysis-input/weather")

print(weather.location)
print(weather.temperature.shape)
```

Prepare an EPW file in a caller-owned output directory:

```python
from MoosasPy.simulation.weather import prepare_epw

prepared = prepare_epw("custom.epw", "analysis-input/weather")

weather = prepared.weather
cumulative_skies = prepared.cumulative_skies
```

`load_epw()` creates the converted weather CSV needed by the native energy
engine. `prepare_epw()` additionally creates WEA and cumulative-sky assets.
Neither function performs station lookup or modifies a packaged database.

## Building-Performance Analysis

Simulation domains are available under `MoosasPy.simulation`:

```python
from MoosasPy import simulation
```

Native runners share a common contract. A runner returns a frozen
`SimulationResult` subclass containing domain results, command diagnostics,
warnings, and a workspace report.

### Energy Analysis

`EnergyRunner` runs the residential or public energy model directly in Python.
It returns an `EnergyResult` with structured energy data:

```python
from MoosasPy.simulation.energy import EnergyRunner
from MoosasPy.simulation.weather import load_epw

weather = load_epw("custom.epw", "analysis-input/weather")
result = EnergyRunner(
    model=model,
    weather=weather,
    temporal_scale="daily",
    spatial_scale="zone",
).run()

print(result.data["total"])
```

`temporal_scale` accepts exactly one of `"monthly"`, `"daily"`, or
`"hourly"`; its default is `"monthly"`. `spatial_scale` accepts
`"building"` or `"zone"` and defaults to `"building"`. Results expose only
the requested temporal scale, with an additional zone-scale field when zone
output is requested.

Other energy options include the building type (`core`), radiation mode, and
schedule path. `core` accepts `buildingType.RESIDENTIAL`, `OFFICE`, `HOTEL`,
`SCHOOL`, or `COMMERCIAL`. Apply the matching climate-zone building template
to each space before simulation; the non-residential types share the public
building calculation path and use their templates and schedules for distinct
occupancy, equipment, and lighting profiles.

Radiation modes are:

- `0` or `False`: fast geometry-based estimate.
- `1` or `True`: consume precomputed seasonal radiation totals.
- `2`: consume precomputed radiation schedules.

Use `build_energy_input()` when direct access to the generated zone parameters
and engine configuration is necessary.

### Radiation Calculation

The fast radiation API uses the packaged Moosas radiation engine:

```python
from MoosasPy.simulation.radiation import modelRadiation
from MoosasPy.simulation.weather import prepare_epw

prepared = prepare_epw("custom.epw", "analysis-input/weather")
modelRadiation(model, prepared.cumulative_skies, reflection=0)
```

The radiation package exposes calculations at several scales:

| Function | Purpose |
| --- | --- |
| `modelRadiation` | Update seasonal radiation values for every space. |
| `spaceRadiation` | Calculate seasonal radiation for one space. |
| `faceRadiation` | Calculate sky-patch visibility or radiation for a face grid. |
| `positionRadiation` | Calculate cumulative radiation at directed positions. |
| `rayTest` | Test ray intersections and reflections against a model or GEO scene. |
| `positionSunHour` | Calculate direct sunlight duration. |

`positionRadiation()` and `rayTest()` require either a model or an explicit GEO
scene path. Batch rays into one call when possible to reduce native-process
overhead.

### Radiance Daylight Analysis

`RadianceRunner` performs a daylight calculation in an isolated workspace:

```python
from datetime import datetime

from MoosasPy.simulation.radiation import RadianceRunner, RadianceSky
from MoosasPy.simulation.weather import load_epw

weather = load_epw("custom.epw", "analysis-input/weather")
sky = RadianceSky(
    date=datetime(2026, 6, 21, 12),
    sky_type="+s",
    location=weather.location,
    diffuse_illuminance=15000.0,
)
result = RadianceRunner(model, sky).run()

for floor in result.floors:
    print(floor.uid, floor.daylight_factor, floor.satisfied_fraction)
```

`RadianceSky` takes its location from the EPW metadata; latitude and longitude
are not separate simulation inputs.

Each floor result contains grid illuminances, daylight factor, and the fraction
of points exceeding 300 lux. The runner reports the `oconv` and `rtrace`
commands used by the calculation.

### Ventilation Analysis

`MoosasPy.simulation.airflow` accepts a `MoosasModel`, constructs its airflow
network, generates a CONTAM project, and performs buoyancy/sensible-heat
iterations inside one runner call.

Run an airflow simulation directly from a model:

```python
from MoosasPy.simulation.airflow import AirflowRunner

result = AirflowRunner(
    model=model,
    outdoor_temperature=25.0,
    max_iterations=50,
    convergence_tolerance=0.01,
).run()

print(result.converged, result.iteration_count, result.residual)
for zone in result.zones:
    print(zone.user_name, zone.temperatures, zone.ach_values)
```

`AirflowResult` contains the final airflow matrix, immutable per-zone histories,
convergence state, native-command diagnostics, warnings, and the retained
workspace report. Intermediate network JSON, CONTAM projects, and iteration
steps are implementation details rather than public simulation inputs.

Bundled CONTAM executables support Windows x86-64 and Linux x86-64. Other
architectures fail explicitly.

### Coupled Workflows

Cross-domain orchestration belongs to `MoosasPy.simulation.coupling`; energy,
radiation, airflow, and weather modules do not import one another directly.

Energy with calculated radiation:

```python
from MoosasPy.simulation.coupling import run_energy_with_radiation
from MoosasPy.simulation.weather import prepare_epw

prepared = prepare_epw("custom.epw", "analysis-input/weather")
data = run_energy_with_radiation(
    model,
    weather=prepared.weather,
    cumulative_skies=prepared.cumulative_skies,
    radiation_mode=1,
    reflection=0,
)
```

The coupling package also provides:

- `EnergyAirflowCoupler` for coupled thermal-load and airflow workflows.
- `run_roof_pv` and `run_facade_pv` for hourly photovoltaic generation.
- `calculate_face_incident_energy` for face-level incident solar energy.

`EnergyAirflowCoupler` delegates every hourly airflow solve to
`AirflowRunner`. Its `iteration` argument sets the maximum internal iterations;
CONTAM project paths and individual iteration functions are not coupling API
inputs.

Example photovoltaic calculation:

```python
from MoosasPy.simulation.coupling import run_roof_pv
from MoosasPy.simulation.weather import prepare_epw

prepared = prepare_epw("custom.epw", "analysis-input/weather")
hourly_generation = run_roof_pv(
    model,
    prepared.cumulative_sky_matrix,
    useful_area_ratio=0.7,
    efficiency=0.17,
)
```

## Native Engines and Workspaces

Energy, Radiance, and airflow runners execute native commands through the
`NativeEngine` protocol. `SubprocessEngine` is the default local
implementation; tests and integrations can provide another engine explicitly.

Each calculation uses `SimulationWorkspace` to isolate intermediate files:

- Temporary workspaces are cleaned automatically.
- A caller-provided root or a retained workspace remains available after the
  calculation.
- `WorkspaceReport.path` identifies the workspace.
- `WorkspaceReport.retained` states whether it remains on disk.

Native resources must remain in their packaged locations under `MoosasPy/libs`.
Do not move individual executables away from their support files.

## File Formats Used by Native Tools

The stable public interfaces generate native input files automatically. The
main internal formats are:

| File | Consumer | Contents |
| --- | --- | --- |
| `.geo` | Geometry transform and radiation engine | Categorized polygon faces, normals, and vertices. |
| `.net` | Native AFN project builder | Zones, openings, topology, pressure, and boundary data. |
| `.prj` | CONTAM | Multizone airflow project. |
| `.info` | Airflow iteration | CONTAM zone name, heat load, and user zone name. |
| `.rad` / `.oct` | Radiance | Scene description and compiled octree. |

These are internal execution contracts. Prefer the Python builders and runners
unless another tool must exchange one of these files directly.

## Runtime Resources

The installed package expects the following directories to remain adjacent to
the Python modules:

- `db/`: building templates, schedules, weather records, material data, the
  EnergyPlus 26.1 IDD, and the default IDF template.
- `libs/rad/`: the Moosas radiation engine and Radiance executables/resources.
- `libs/vent/`: AFN tools and platform-specific CONTAM distributions.
- `libs/weather/`: EPW-to-WEA and cumulative-sky executables/resources.

The Python distribution includes these resources through `pyproject.toml` and
`MANIFEST.in`.

## Testing

Tests and fixtures belong under the repository-level `test/` directory. Run the
full suite from the repository root:

```bash
python -m pytest -q
```

The suite covers geometry regressions, model I/O, resource packaging,
EnergyPlus version enforcement, native runners, simulation workspaces, weather
architecture, and domain boundaries.

## Packaging and Release

Build and validate distributions from the repository root:

```bash
python -m build
python -m twine check dist/*
```

The accepted release tag format is:

```text
moosaspy-vX.Y.Z
```

On Windows, the release helper can validate and publish a tag:

```powershell
.\scripts\release_moosaspy.ps1 1.2.2
```

The GitHub Actions release workflow builds the wheel and source distribution,
creates a GitHub Release, and uploads both artifacts. GitHub Packages does not
provide the PyPI-compatible registry used by this project; install a release
wheel through its GitHub Release URL or publish the same distributions to PyPI.

Use the release helper's `-DryRun` option to preview tag operations. Replacing
an existing release requires `GH_TOKEN` or `GITHUB_TOKEN` with repository
contents permission so the helper can remove the previous GitHub Release before
the workflow uploads artifacts with the same names.

## Credits and Acknowledgements

Developed by the research team directed by **Prof. Borong Lin** at the Key
Laboratory of Eco Planning & Green Building, Ministry of Education, Tsinghua
University.

For collaboration: [linbr@tsinghua.edu.cn](mailto:linbr@tsinghua.edu.cn)

For technical questions: [junx026@gmail.com](mailto:junx026@gmail.com), [liyihui23@mails.tsinghua.edu.cn](mailto:liyihui23@mails.tsinghua.edu.cn)
