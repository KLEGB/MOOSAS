# MOOSAS

MOOSAS is a building-performance analysis and optimization toolkit for the
early design stage. This repository contains **MoosasPy**, its Python package
for geometry transformation, model exchange, and building-performance
simulation workflows.

## Features

- Transform GEO, OBJ, and STL geometry into a structured `MoosasModel`.
- Load and save RDF/Turtle, XML, JSON, and EnergyPlus IDF models.
- Export graph JSON and gbXML, with dedicated IFC conversion utilities.
- Analyze energy, radiation, daylight, and airflow, with explicit coupled
  workflows.
- Prepare weather data and manage native simulation workspaces.

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

For model formats, thermal settings, simulation workflows, and supported
exporters—including OpenFOAM—see the [MoosasPy usage documentation](doc/document.md).

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
