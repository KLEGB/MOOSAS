# Geometry Transformation

`MoosasPy.transform` accepts GEO, OBJ, and STL geometry sources and produces a
structured `MoosasModel`. It does not load or save complete semantic model
formats.

| Module | Responsibility |
| --- | --- |
| `pipeline.py` | Geometry source to complete `MoosasModel` pipeline |
| `options.py` | Configuration for the transformation pipeline |
| `importers/` | GEO, OBJ, and STL readers |
| `stages/` | Classification, cleanup, generation, assembly, and topology |
| `geometry/` | Geometry types and transformation algorithms |
| `alignment/` | Geometry-to-model alignment helpers |

The pipeline decides whether to split intersecting walls and remove resulting
duplicates; the corresponding stages perform those operations without option flags.
After boundary generation, the pipeline packs edges and selects core/perimeter
partitioning when `insert_core` is enabled, convex partitioning when only
`divided_zones` is enabled, or no partitioning otherwise, before assembling spaces.
It also selects boundary simplification, source-face decomposition, vertical wall
splitting, cleansing operations, void connection, shading, and standardization.
Space assembly still receives `solve_overlap` because it controls both horizontal
intersection processing and floor capping during floor matching.

Complete model formats are owned by `MoosasPy.model.io` and exposed through
`MoosasModel.load()` and `model.save()`.

## Dependency boundaries

1. Geometry files enter the package through `transform()`.
2. The transformation pipeline produces a complete `MoosasModel`.
3. Geometry algorithms do not depend on model I/O or simulation modules.
4. Simulation consumes a `MoosasModel` and does not invoke transformation.
