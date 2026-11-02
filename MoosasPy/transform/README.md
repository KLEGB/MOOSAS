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

## Offset thermal zoning

`TransformOptions(insert_core=True)` now partitions each occupied footprint into
offset perimeter/core thermal zones during space assembly, even when
`divided_zones=False`. It no longer inserts a cube, physical core walls, slab
openings, or an unconditioned shaft. The old `inject_minimal_core` implementation
has been removed; direct callers must migrate. Zone counts, IDs, and energy
results can change. With `insert_core=False`, existing subdivision is unchanged.

Configure `geom.CORE_OFFSET_DEPTH` (default 5m) and `geom.CORE_MIN_WIDTH` (default
2m) internally in `MoosasPy.utils.constant`; these are not transform arguments.
Narrow offset branches/necks collapse to original-length centerlines, never
extended to the exterior. These are zero-width hole constraints, not invalid
zero-area polygon rings. Visible auxiliary edges open the constraints into real
regions before the existing convex splitter runs. Every centerline interval must
border two faces; no face may cross it. New internal walls are AirBoundary walls.

The medial axis uses 0.1m boundary samples, 0.05m line simplification, 5mm offset
boundary cleanup, and a 0.2mm width-threshold tolerance. Empty eroded cores are
valid: the whole footprint remains perimeter. Invalid footprints or failed
constraints raise `GeometryError`, without triangulation fallback.

Small/slender partition merging uses the existing thresholds independently for
perimeter and core, and cannot delete centerlines. Final spaces may be concave.
Small regions with no eligible neighbor are preserved with a warning.
