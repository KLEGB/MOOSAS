# MOOSAS

MOOSAS is a building-performance analysis and optimization toolkit for the
early design stage. This repository contains **MoosasPy**, its Python package
for geometry transformation, model exchange, and building-performance
simulation workflows.

## Features

- Transform GEO, OBJ, and STL geometry into a structured `MoosasModel`.
- Load and save RDF/Turtle, XML, JSON, and EnergyPlus IDF models.
- Export graph JSON and gbXML, with dedicated IFC conversion utilities.
- Export a selected room as an OpenFOAM volume mesh through `exportFoam`.
- Prepare weather data and cumulative sky models from user-provided EPW files.
- Run rapid energy, solar-radiation, sunlight, Radiance daylight, and CONTAM
  airflow analyses.
- Coordinate weather, radiation, photovoltaic, energy, and airflow domains
  through explicit coupling workflows.
- Isolate native simulations in managed workspaces with structured results and
  command diagnostics.

## Requirements

- Python 3.10 or newer
- EnergyPlus 26.1 IDF input when using the IDF adapters
- A supported platform for workflows that invoke packaged native executables

Bundled CONTAM tools support Windows x86-64 and Linux x86-64. Native-tool
availability should be validated on the target platform before deployment.

## Installation

Install MoosasPy from the repository root:

```bash
python -m pip install .
```

For development, tests, and distribution tooling:

```bash
python -m pip install -e ".[dev]"
```

Verify the installation:

```bash
python -c "import MoosasPy; print(MoosasPy.__version__)"
```

## Quick Start

Transform geometry and save the resulting model:

```python
from MoosasPy.transform import TransformOptions, transform

options = TransformOptions(attach_shading=True)
model = transform("building.geo", options=options, stdout=None)

print(len(model.spaceList))
model.save("building.ttl")
```

With `TransformOptions(divided_zones=True)`, the existing convex decomposition
is followed by a zone-only merge: parts below `geom.ROOM_MIN_AREA` (1 m2), or
slender parts whose minimum rotated bounding rectangle has a short side below
`geom.PARTITION_SLIVER_MAX_WIDTH` (0.1 m) and a long-to-short-side ratio above
`geom.PARTITION_SLIVER_MIN_ASPECT_RATIO` (20), are absorbed into the neighbor
sharing the longest edge, even if the union is concave. Width and aspect ratio
thresholds are strict and independent of the part's orientation. The union must
remain valid, connected, and hole-free; unmergeable parts are retained with a
warning. Other parts are not merged merely because their union is convex.
The original `split_poly` and hole decomposition implementations are unchanged.
This rule applies only to thermal-zone partitions, not to physical face
fragments created later by floor/ceiling intersections.

Horizontal face splitting and floor/ceiling matching use the squared coordinate
precision to retain small physical fragments. Skylights use the same area
tolerance when determining whether they belong to a split face or cross its
boundary; crossing skylights are split between their parent faces.

When assembling spaces, immediately nested contours are excluded from the
parent's floor/ceiling matching footprint before missing floor portions are
capped. Courtyards are therefore not filled with air-boundary faces; real
uncovered portions within the room footprint still receive air boundaries.
This applies with or without `divided_zones`.

Load a complete model without rerunning geometry transformation:

```python
from MoosasPy import MoosasModel

model = MoosasModel.load("building.ttl")
model.summary()
```

Prepare an EPW file, then run energy analysis with the resulting weather object:

```python
from MoosasPy.simulation.energy import EnergyRunner
from MoosasPy.simulation.weather import load_epw

weather = load_epw("custom.epw", "analysis-input/weather")
result = EnergyRunner(model=model, weather=weather).run()
print(result.data["total"])
```

Energy simulation uses the bundled MOOSAS Energy CES3 v2 binary engine with explicit
surface boundaries (`outdoors`, `ground`, `adiabatic`). Each space is an
independent single thermal zone; shared walls/slabs retain their allocated
thermal storage with an adiabatic back face. See
[the energy contract](MoosasPy/libs/moosas_energy/README.md) for parameter
overrides, hourly radiation input, restart state, and native build instructions.
Run `python -m pytest test/test_example_integration.py -k energy_engine -q`
for a full-year geometry-fixture check with the packaged `MoosasEnergy` executable.

Thermal templates are managed in `MoosasPy/db/building_template.json`, with
GB 55015—2021 limits separated from equivalent-material assumptions. Select a
profile using `EnergyRunner(..., template_id="public_A_cold_GB55015-2021_OFFICE")`;
an optional `template_path` loads a project library. See
[the thermal template guide](MoosasPy/db/THERMAL_TEMPLATES.md) for climate/class
selection, explicit parameter precedence, and binary-engine acceptance.

## OpenFOAM Volume Mesh Export

Export a selected constant-section room with the dedicated OpenFOAM function:

```python
from MoosasPy.transform import transform
from MoosasPy.model.io.foam import exportFoam

model = transform("test/caseFile/test0_6spacesIntersection.geo", input_type="geo")
result = exportFoam(model, "cases/room/room.foam", space_index=0, grid_size=0.5, layers=12)
print(result.primary_path)  # cases/room/room.foam
```

This writes a ParaView marker, five mesh files and a `constant/moosasGrid.json`
cell-to-grid mapping. The volume covers the complete footprint, including
clipped perimeter cells and holes; its base is the source face, independent of
the sampling height. All boundary patches default to walls. Use metres for
geometry and supply your own field boundary conditions and solver settings.

This exporter supports a single horizontal floor and constant room height.
It does not support arbitrary room solids, varying ceiling heights, internal obstacles,
or automatic window/door patches. Existing meshes are not overwritten.
See [OpenFOAM export details](doc/openfoam.md) for parameters, mapping semantics,
and `checkMesh` validation.

For an isothermal indoor or outdoor CFD simulation, use `exportFoam`
with `scenario="indoor"` or `scenario="outdoor"` and explicit
`conditions`. It writes a complete OpenFOAM Foundation 12 case:

```python
from MoosasPy.model.io.foam import exportFoam

conditions = {
    "viscosity": 1.5e-5,
    "turbulence_intensity": 0.05,
    "turbulence_length": 1.0,
    "iterations": 1000,
    "velocity": [2.0, 0.0, 0.0],
    "domain": [[-100.0, 120.0, 0.0], [0.0, 240.0, 45.0]],
    "inside_point": [-90.0, 130.0, 5.0],
}
saved = exportFoam(model, "cases/wind/case.foam", scenario="outdoor",
                   grid_size=4, conditions=conditions)
print(saved.primary_path.parent)  # OpenFOAM case directory
```

See the [GEO-to-CFD guide](doc/openfoam.md) for runnable indoor/outdoor examples,
environment setup, opening IDs, physical assumptions, and real-GEO verification.
Run meshing and solving with OpenFOAM's own command-line tools or workflow.

## Model and Simulation Domains

| Area | Public entry point |
| --- | --- |
| Geometry transformation | `MoosasPy.transform` |
| Complete model I/O | `MoosasPy.MoosasModel` |
| Energy | `MoosasPy.simulation.energy` |
| Radiation and daylight | `MoosasPy.simulation.radiation` |
| Airflow and CONTAM | `MoosasPy.simulation.airflow` |
| Weather and sky models | `MoosasPy.simulation.weather` |
| Cross-domain workflows | `MoosasPy.simulation.coupling` |

Raw GEO, OBJ, and STL files enter through `transform()`. Complete RDF/Turtle,
XML, JSON, and IDF models enter through `MoosasModel.load()`. MoosasPy reads and
writes EnergyPlus 26.1 IDFs with its bundled 26.1 `Energy+.idd`; older IDFs must
be migrated with the official EnergyPlus Transition chain first.

XML, JSON, and IDF loading supports both single-room and multi-room models,
including a single window or skylight record.

See the [MoosasPy documentation](doc/document.md) for transformation options,
supported model formats, thermal settings, simulation APIs, native resources,
and release instructions.

## Development

Run the test suite from the repository root:

```bash
python -m pytest -q
```

Build and validate distributions:

```bash
python -m build
python -m twine check dist/*
```

Versions are derived from Git tags matching `moosaspy-vMAJOR.MINOR.PATCH`.
Pushing a matching tag triggers the GitHub Actions release workflow, which
builds the wheel and source distribution and uploads them to a GitHub Release.

## License

MoosasPy is distributed under the Apache License 2.0. See [LICENSE](LICENSE).

## Credits and Contact

Developed by the research team directed by **Prof. Borong Lin** at the Key
Laboratory of Eco Planning & Green Building, Ministry of Education, Tsinghua
University.

For collaboration: linbr@tsinghua.edu.cn

For technical questions: junx026@gmail.com, liyihui23@mails.tsinghua.edu.cn

Annual Radiance daylight (DA, UDI, area-weighted sDA and direct-sun ASE),
including TTL optical/airflow properties and runnable NBuilding verification,
is documented in [doc/annual_daylight.md](doc/annual_daylight.md).
