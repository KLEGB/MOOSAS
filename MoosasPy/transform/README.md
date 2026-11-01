# Transformation Boundary

`MoosasPy.transform` accepts GEO, OBJ, and STL geometry sources and produces a
structured `MoosasModel`. It does not load or save semantic model formats.

| Module | Responsibility |
| --- | --- |
| `pipeline.py` | Geometry source to complete `MoosasModel` pipeline |
| `importers/` | GEO, OBJ, and STL source readers |
| `stages/` | Classification, cleanup, generation, assembly, and topology |
| `geometry/` | Geometry types and transformation algorithms |
| `alignment/` | RDF/IDF graph linking and multi-file alignment |

Model formats are owned by `MoosasPy.model.io` and exposed only through
`MoosasModel.load()` and `model.save()`.

Dependency rules:

1. Geometry files enter through `transform()` only.
2. Transform stages return a complete `MoosasModel`.
3. Geometry algorithms do not import model I/O or simulation.
4. Simulation consumes `MoosasModel` and does not invoke transform.

## Faces with holes

Both face-convexification entry points use double cuts instead of constrained
triangulation for polygons with holes. For each hole, the shortest visible
vertex-to-vertex connector is paired with the shortest admissible second
connector that does not touch the first. Polygonizing the boundary and both cuts
produces real regions without repeated bridge edges. After all holes are opened,
the existing simple-polygon convex splitter processes each region.

The decomposition checks validity, convexity, hole preservation, area coverage,
and absence of area overlaps. Invalid inputs, missing connector pairs, and failed
convex splits raise `GeometryError`; there is no triangulation fallback. Public
signatures are unchanged, but face/zone counts and generated IDs may differ.
Hole-free splitting and OpenFOAM mesh triangulation are unchanged.
