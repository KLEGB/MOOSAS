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
|   |-- boundary.py        MoosasFloor/MoosasEdge, aggregated space boundaries
|   |-- container.py       Model-level collections and geometry registry
|   |-- space.py           MoosasSpace, enclosure topology, and zone settings
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
|-- db/                    Templates, weather, and EnergyPlus data
|-- libs/                  Packaged native executables and support files
`-- __temp__/              Runtime workspace; not included in distributions
```

The three main public API areas are:

| API | Purpose |
| --- | --- |
| `MoosasPy.transform` | Convert GEO, OBJ, or STL geometry into a `MoosasModel`. |
| `MoosasPy.MoosasModel` | Load, inspect, edit, and save complete building models. |
| `MoosasPy.simulation` | Run energy, radiation, weather, airflow, and coupled workflows. |

`MoosasSpace` is defined in `MoosasPy.model.space` and exported by
`MoosasPy.model`. Import it with `from MoosasPy.model import MoosasSpace`;
the former `MoosasPy.transform.geometry.element.MoosasSpace` import path has
been removed.

`MoosasFloor` and `MoosasEdge` are defined in `MoosasPy.model.boundary` and
exported by `MoosasPy.model`. Import them with
`from MoosasPy.model import MoosasFloor, MoosasEdge`; their former
`MoosasPy.transform.geometry.element` import paths have been removed.
Geometry surfaces remain in `MoosasPy.transform.geometry.element`.

XML serialization belongs to `MoosasPy.model.io.xml`, not the domain classes.
The former object `to_xml` methods have been removed. Use `build_xml(model,
write_geometry=False)` for a complete model, or `element_to_xml`,
`floor_to_xml`, `edge_to_xml`, and `space_to_xml` for individual objects.
XML/JSON file formats and `MoosasModel.save` remain unchanged. Legacy
`fromDict` construction and `MoosasSpace.to_string` are not changed by this
serialization relocation.

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
| `insert_core` | `False` | Generate offset perimeter/core thermal zones with opaque core/perimeter interfaces; automatically enables spatial division. Other calculation partitions remain AirBoundary surfaces. |
| `standardize` | `False` | Standardize the finalized model geometry. |

With `insert_core=True`, boundaries between actual core regions and perimeter
regions are opaque, paired interzone walls using the existing wall thermal
settings. They do not use AirBoundary `SimpleMixing`. Convex subdivisions
inside either region and zero-width centerlines from collapsed narrow cores
remain air boundaries. This changes boundary physics, not zoning geometry or
space-type recognition. Existing cached models and IDFs retain their old
categories; regenerate from source geometry to apply this rule.

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

Space topology nodes active walls at shared endpoints before recording adjacency.
Partially coincident walls are split into matching segments and shared by the
adjacent spaces, rather than exported as two outdoor walls. Physical walls take
precedence over coincident virtual cuts. Wall splitting retains thermal settings,
categories and glazing settings. The projected union of an element's source
faces determines its area, so dissolved duplicate faces do not inflate
area-based infiltration. Source geometry IDs are retained for alignment.

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
- `schedule`: explicitly imported schedule definitions (gbXML, RDF, user-defined).

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

#### OpenFOAM Mesh and CFD Export

OpenFOAM export uses `exportFoam()`, not `model.save()`. The `.foam` target is
a ParaView marker; its parent directory is the generated case directory.
Exporting does not require OpenFOAM. Meshing and solving CFD cases require an
OpenFOAM Foundation 12 installation.

To export a single-room volume mesh, select a room by its zero-based index:

```python
from MoosasPy.transform import transform
from MoosasPy.model.io.foam import exportFoam

model = transform("test/caseFile/test0_6spacesIntersection.geo", input_type="geo")
saved = exportFoam(
    model,
    "cases/room/room.foam",
    space_index=0,
    grid_size=1.0,
    layers=8,
)
print(saved.primary_path)
```

The room must have one horizontal floor face and a constant height. The output
contains an extruded `constant/polyMesh`, a `constant/moosasGrid.json` mapping,
and the `.foam` marker. This mesh-only mode does not create solver settings or
initial fields.

For an indoor CFD case, provide one inlet and one outlet belonging to the
selected room:

```python
from MoosasPy.transform import transform
from MoosasPy.model.io.foam import exportFoam

model = transform("test/caseFile/test6_twoVolumes.geo", input_type="geo")
conditions = {
    "viscosity": 1.5e-5,
    "turbulence_intensity": 0.05,
    "turbulence_length": 1.0,
    "iterations": 1000,
    "inlet": {"opening": "gls_g_80", "velocity": [0.409451, 0.286966, 0.0]},
    "outlet": {"opening": "gls_g_79", "pressure": 0.0},
}
saved = exportFoam(
    model,
    "cases/indoor/case.foam",
    scenario="indoor",
    space_index=37,
    grid_size=2.0,
    conditions=conditions,
)
```

The opening identifiers above are from the documented example model; inspect
your own room's glazing and skylight IDs before using them. Inlet velocity is
in world coordinates in m/s and must point into the room. Outlet pressure is
kinematic gauge pressure in m²/s². Unspecified openings remain closed.

For outdoor wind flow, provide the inflow velocity, domain bounds, and an
air point outside the building:

```python
from MoosasPy.transform import transform
from MoosasPy.model.io.foam import exportFoam

model = transform("test/caseFile/test0_6spacesIntersection.geo", input_type="geo")
conditions = {
    "viscosity": 1.5e-5,
    "turbulence_intensity": 0.05,
    "turbulence_length": 1.0,
    "iterations": 1000,
    "velocity": [2.0, 0.0, 0.0],
    "domain": [[-100.0, 120.0, 0.0], [0.0, 240.0, 45.0]],
    "inside_point": [-90.0, 130.0, 5.0],
}
saved = exportFoam(
    model,
    "cases/outdoor/case.foam",
    scenario="outdoor",
    grid_size=4.0,
    conditions=conditions,
)
```

Both CFD scenarios also require `viscosity` (m²/s), `turbulence_intensity`
(fraction), `turbulence_length` (m), and `iterations`. They create a steady,
isothermal, incompressible RANS case using k-epsilon. MoosasPy writes the case
but does not run mesh generation or the solver. In the case directory, run:

```sh
surfaceCheck constant/geometry/model.stl
blockMesh
snappyHexMesh -overwrite
checkMesh -allTopology -meshQuality
foamRun -solver incompressibleFluid
```

The CFD cases assume smooth ground and uniform inflow. Atmospheric boundary
layers, surface roughness, temperature, buoyancy, and contaminant transport are
not modeled. CFD targets must use an empty case directory to avoid mixing old
results with the new case.

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

New spaces do not automatically select a building template. Select a climate
zone and building type explicitly:

```python
from MoosasPy.model import apply_building_template

apply_building_template(
    model,
    "CN/GB55015-2021/3A/Office",
)

model.spaceList[0].settings["zone_equipment"] = 8.8
model.spaceList[0].explicit_settings.append("zone_equipment")
```

`space.applySettings()` accepts only a complete template ID already registered
in `model.buildingTemplate`; dictionaries and regex hints are not supported.
Energy, airflow, and daylight simulations require an explicitly selected
BuildingTemplate. Template values are resolved once per space when constructing
simulation inputs, with explicit model edits and runtime overrides taking
precedence. `template_overrides_model=True` replaces model edits but not runtime
overrides. Unconditioned spaces retain disabled HVAC.

The model stores one portable snapshot per selected template, while spaces store
only `zone_template` and explicit parameter edits. XML/JSON/RDF energy metadata
uses schema version 3 and embeds the used templates, including resolved schedules,
once at model level. Older metadata and per-space snapshots are rejected without
a compatibility path. Recreate those model files from source geometry and apply
templates explicitly. `template_program` and `apply_building_template_to_space`
have been removed; use `apply_building_template` or `space.applySettings`.

Important energy-setting fields include:

| Field | Meaning |
| --- | --- |
| `zone_wallU` | Opaque-envelope U-value in W/(m²·K). |
| `zone_winU` | Window U-value in W/(m²·K). |
| `zone_win_SHGC` | Window solar heat-gain coefficient. |
| `zone_c_temp`, `zone_h_temp` | Cooling and heating set points in °C; `null` disables that mode. |
| `hvac_enabled` | Total HVAC operating permission, from template `hvac_availability` unless explicitly overridden; 0 off / 1 on. |
| `zone_collingEER`, `zone_HeatingEER` | Cooling and heating efficiency values. |
| `zone_ppsm` | Effective hourly occupant density in people/m². |
| `zone_pfav` | Effective outdoor-air requirement per person after availability schedule. |
| `zone_equipment`, `zone_lighting` | Effective hourly gains in W/m² after usage schedules. |
| `zone_infiltration` | Infiltration rate, optionally varying through its 8760-hour schedule. |
| `zone_template` | Applied template identifier. |
| `zone_room` | Room-group key for a multi-room template. |

Building templates use a catalog maintained in the independent
[MOOSAS-BuildingTemplate repository](https://github.com/KLEGB/MOOSAS-BuildingTemplate)
and a shared runtime adapter in `MoosasPy/model/building_templates.py`.
The package bundles only the Office 3A template and its required
office schedule in `MoosasPy/db/template/`. New spaces do not
automatically select or apply a template. Select the namespaced identifier
explicitly, or provide another catalog via `template_path` to
`apply_building_template` or `library_path` to
`load_building_template_library`. The catalog loader resolves and validates
referenced schedules when loading.

```text
MoosasPy/db/template/
  library.json
  CN_GB55015-2021/
    Office_3A.json
    schedules/
      office.sch
```

Each record contains room groups directly under `BuildingTemplate.Space`, with
`air_exchange`, `internal_gains`, and `system` parameters per room; `Envelope`
contains the shared construction groups. Set `zone_room` on every affected
space before applying a multi-room template.
Single-room templates use `default`; multi-room templates require an explicit
key such as `outpatient`, `inpatient`, or a residential room type. Parameters
use `{mode: "constant", value, unit}` or
`{mode: "schedule", file, name, unit}`. Schedule files are JSON `.sch`
documents with named, unit-labelled arrays of exactly 8760 hourly
values. `null` is allowed only for temperature setpoints and means that mode is
off. The loader resolves schedules and stores the complete template record in
the model snapshot; catalogs and snapshots do not require a version field.
Occupancy density, equipment power density, and lighting
power density are each multiplied by their own hourly usage schedule. Fresh-air
rate is multiplied by its availability schedule. The single `hvac_availability`
follows GB table C.0.6-1; heating/cooling setpoints follow table C.0.6-2.
Both modes require total HVAC permission and a non-null setpoint. Temperature
targets are preserved outside operating hours, but do not trigger control while
HVAC is off, including office holiday heating at 5 °C. Explicit `hvac_enabled`
overrides replace the template permission; changing a setpoint does not change
it. Separate heating/cooling availability fields and zone overrides are not
supported; old snapshots must be updated before use.
The catalog files contain direct construction values;
Energy maps U-value, R-value, and SHGC into its surface model with the declared
package assumptions. Visible reflectance/transmittance remain separate from
solar optics.
Occupant sensible heat is a built-in simulation assumption of 75 W/person,
not a template parameter. Templates do not contain `occupant_heat`, and resolved
zone inputs do not contain `zone_popheat`; runtime overrides of it are rejected.
Source-imported occupant heat metadata does not change this simulation assumption.
Daylight scenes use these visible template properties unless the element has an
explicit property or the request supplies an optical override. Shared surfaces
with conflicting template optics require an explicit override. Annual occupancy uses the same schedule-multiplied occupancy density as energy
simulation.
Construction leakage and opening-ratio properties are preserved in template
records but are not consumed by AFN yet. Leakage conversion requires a declared
reference pressure, which the current catalog does not provide; no reference
pressure is guessed. AFN opening-ratio integration is also deferred. Only
direct catalogs with the required metadata and structure are accepted; old
identifiers and flattened catalog formats have no aliases or compatibility
path.

The default catalog uses stable IDs of the form
`CN/GB55015-2021/<climate-zone>/<building-type>`. It does not infer climate
zones or borrow another country's assumptions.
Templates contain direct construction values and operating schedules. The
runtime does not support table projections, conditional rule selection, or
prescriptive WWR/skylight checks. Different standards need not share
building-type names.

`template_path` means a catalog directory containing `library.json`, not a
single JSON file or a standard-package directory. Earlier catalog resources,
old identifiers, and old template snapshots are not automatically migrated.
Replace template resources explicitly and reapply them to affected models.
The bundled catalog is loaded directly from `MoosasPy/db/template/`; no
download or user cache is needed. To use another catalog, supply its local
checkout through `template_path` or `library_path`.
Versionless template snapshots round-trip through RDF/XML/JSON without the
source catalog or `.sch` files.
The shared model metadata payload in `MoosasPy/model/metadata.py` also
round-trips `model.metadata` alongside energy settings, schedules, and template
snapshots.

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

Other energy options include the building type (`core`) and radiation mode.
`core` accepts `buildingType.RESIDENTIAL`, `OFFICE`, `HOTEL`,
`SCHOOL`, or `COMMERCIAL`. Apply the matching climate-zone building template
to each space before simulation; the non-residential types share the public
building calculation path and use their template operation profiles for
distinct occupancy, equipment, and lighting schedules.

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

`RadianceRunner` provides point-in-time and annual daylight calculations. A
point-in-time calculation uses an explicit sky:

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
of points exceeding 300 lux.

For an annual calculation, prepare a full-year WEA from EPW weather and pass it
to `AnnualDaylightRequest`:

```python
import numpy as np

from MoosasPy.simulation.radiation import AnnualDaylightRequest, RadianceRunner
from MoosasPy.simulation.weather import convert_epw_to_wea

wea_file = convert_epw_to_wea("custom.epw", "analysis-input/custom.wea")
request = AnnualDaylightRequest(
    wea_file,
    output_dir="results",
    grid_spacing_m=0.5,
    workplane_height_m=0.76,
    processes=2,
)
result = RadianceRunner(model).run_annual(request)

total = np.load(result.illuminance_total_file, mmap_mode="r")
direct = np.load(result.illuminance_direct_file, mmap_mode="r")
print(total.shape, result.manifest_file)
```

The result matrices are file-backed with shape `(sensors, 8760)`; use memory
mapping for large annual results. The manifest records resolved inputs,
assumptions, and metric settings. Successful runs retain their output directory.

Common request options include:

| Option | Default | Description |
| --- | ---: | --- |
| `grid_spacing_m` | 0.5 | Clipped horizontal sensor-grid spacing in metres. |
| `workplane_height_m` | 0.76 | Sensor height above the floor in metres. |
| `sky_subdivision` | 2 | Reinhart sky subdivision. |
| `north_rotation_deg` | 0 | Counter-clockwise model north rotation. |
| `ambient_bounces` | 5 | Ambient reflection depth. |
| `ambient_divisions` | 4096 | Ambient sampling divisions. |
| `processes` | 1 | Number of native calculation processes. |
| `space_ids` | `None` | Optional tuple of spaces to analyze. |
| `occupied_by_space` | `None` | Per-space 8760-entry boolean occupancy masks. |
| `optical_overrides` | `None` | Per-element overrides for visible optical properties. |

When no request mask is supplied, a space can define
`settings["daylight_occupied_mask"]` with 8760 boolean values. An applied
building template instead uses its 8760 occupancy-density schedule. Non-template
spaces may still use the legacy `zone_work_start` and `zone_work_end` daily
interval fallback; it does not infer weekdays or holidays. Zero occupied hours
are reported as such, not as a zero performance score.

By default, DA counts occupied hours at or above 300 lux and cDA uses the
occupied-hour average of illuminance divided by 300 lux, capped at 1. sDA is
the area fraction meeting DA for at least 50% of occupied time. UDI bands are
below 100 lux, 100–2000 lux, and above 2000 lux. ASE is the area fraction with
direct illuminance above 1000 lux for more than 250 occupied hours. Thresholds
can be changed with `da_threshold_lux`, `required_time_fraction`,
`udi_lower_lux`, `udi_upper_lux`, `ase_threshold_lux`, and `ase_hours`.
Per-space sDA and ASE are area-weighted; ratios are represented from 0 to 1.

Annual analysis requires an ordered, complete, hourly, non-leap WEA with 8760
records and valid DNI/DHI data. The analysis uses the model's fixed shading
state and scalar, achromatic visible properties. It does not operate blinds
and is not a full LM-83 operational-shading assessment; BSDF and colored
optical materials are not supported. Visible transmittance, solar
transmittance, and SHGC are distinct properties. Opaque surfaces use
`visible_reflectance`; windows and skylights use `visible_transmittance`.
Missing visible values use defaults recorded in the run manifest: VLT 0.70,
floor reflectance 0.30, ceiling reflectance 0.75, and 0.60 for other opaque
surfaces.

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

- `db/`: weather records, material data, the
  EnergyPlus 26.1 IDD, and the default IDF template.
- `libs/radiance/`: one Radiance installation shared by daylight and weather.
- `libs/contam/`: CONTAM distributions and the simread response template.
- `libs/moosas_rad/`, `libs/moosas_afn/`, `libs/moosas_energy/`: independent
  native engines. Executables are under `<platform>/bin/`; Radiance function
  files are under `<platform>/lib/`.
- `db/radiation/`, `db/airflow/`: solar directions and the pressure model.

Building templates live in the independent repository and the per-user cache,
not adjacent to the Python modules.

Go source and build scripts live directly in `libs/moosas_rad/` and
`libs/moosas_afn/` alongside their runtimes and are included in distributions.
Runtime inputs and outputs
belong in simulation workspaces, never in `libs/`. Old resource paths were
removed without compatibility aliases; external scripts must use the new layout.

The Python distribution includes these resources through `pyproject.toml` and
`MANIFEST.in`.

## Testing

Tests and fixtures belong under the repository-level `test/` directory. The
default run excludes tests marked `slow` (heavy integration and external-solver
runs). Run the fast suite from the repository root:

```bash
python -m pytest -q
```

Run the slow tier, or the full suite, before releases:

```bash
python -m pytest -q -m slow
python -m pytest -q -m "slow or not slow"
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
