# MoosasRad

MoosasRad is the batched ray-to-geometry engine used by MOOSAS radiation workflows. `build.sh` builds and installs both Linux amd64 (`MoosasRad`) and Windows amd64 (`MoosasRad.exe`) executables with `CGO_ENABLED=0` and `GOAMD64=v1`.

## Command line and files

```text
MoosasRad.exe -g geometry.geo -o result.o input.i
```

Each non-empty ray input line is `origin_x,origin_y,origin_z,dir_x,dir_y,dir_z`. The engine normalizes directions. Geometry is stored in `;`-terminated blocks with `fn,nx,ny,nz`, outer vertices `fv,x,y,z`, and optional hole vertices `fh,hole_index,x,y,z`. Hole indices may be sparse. Both LF and CRLF are supported; malformed geometry and zero normals fail the process.

Each output line contains the closest accepted hit point and reflected direction, or six `-1.00` values for a miss. Output ordering matches ray input order; coordinates and directions retain the existing two-decimal format.

## Intersection behavior

Faces with `abs(normal.z) > 1e-6` use the existing XY point-in-ring approximation. Vertical and near-vertical faces use a cached in-plane UV projection. Ring tests include the closing edge; concave polygons are supported. A 0.01 coordinate-unit tolerance is used at projected boundaries. Hole interiors pass rays through; hole boundaries count as solid.

At load time the engine builds a deterministic, 16-bin SAH BVH over face bounds. Nodes and face indices are contiguous arrays shared read-only by workers. Leaves target eight faces; unsplittable or unprofitable nodes remain leaves, and depth is capped at 48. Bounds are expanded by the boundary tolerance and rounded outwards.

Each worker reuses a traversal stack. Slab tests handle axis-parallel rays and origins inside a box; near nodes are visited first, and nodes beyond the closest real hit are pruned. Leaf tests retain the existing plane, projected polygon and hole predicates. Equal-distance hits are resolved by original face index, irrespective of tree traversal order. Only the final hit generates a reflected direction. The reference linear/sorted query is retained for differential tests. Boundary coefficients are cached at load time and output formatting uses a reusable byte buffer.

SketchUp's triangulated compatibility export validates rounded triangles in the same supplied-normal projection as the engine. Degenerate pieces are listed in the adjacent `.geo.export.json`; the engine still rejects invalid geometry instead of silently dropping it. Analysis normal conventions, sky mapping, JSON protocol and MoosasPy algorithms are unchanged.

## Build

Run `./build.sh stage /path/to/candidate` to cross-compile without replacing the installed binaries. Both targets are built in a temporary directory before being moved to the destination; build metadata is printed. Default `./build.sh` installs to this directory. Go 1.21.5 is used for both baseline and candidate, targeting `linux/amd64` and `windows/amd64`, `GOAMD64=v1`, `CGO_ENABLED=0`, with `-trimpath`. The host compiler being `windows/386` does not make the target programs 32-bit.

## Reproducible current-model benchmark

See [BVH_BENCHMARK.md](BVH_BENCHMARK.md) for the measured results, deployment hashes and the two documented pre-existing near-vertical XY tolerance false hits found by native SketchUp comparison.

`skp/scripts/rad_bvh_benchmark.rb` exports the visible full scene and samples 100 face instances, 25 area-weighted interior points each, with seed 4217 and 0.1 m normal offset. Component instance paths distinguish repeated definitions. Selection and visibility are not changed. The frozen scene, weather matrix and sampling manifest are kept in a runtime workload directory.

Run `python/python.exe skp/scripts/benchmark_rad_bvh.py benchmark --workload WORKLOAD --output OUTPUT --baseline BASELINE_EXE --candidate CANDIDATE_EXE --threads 4`. On Windows hybrid-core CPUs add `--affinity-mask MASK` after checking core topology; the benchmark process and all its children inherit the same core mask. The driver warms up both programs and runs five alternating paired repetitions for Sunhour and Radiation. It captures the actual pipeline rays, verifies byte-identical engine outputs and equal JSON results, and measures complete engine calls, Python pipeline execution and Python process wall time separately. Engine redirection is isolated to the benchmark subprocess and does not modify MoosasPy or the installed executable.

For pure-query diagnostics, set `RAD_PROFILE_SCENE`, `RAD_PROFILE_RAYS` and `RAD_PROFILE_OUTPUT`, then run `GO111MODULE=off GOARCH=amd64 go test -run TestBVHRealSceneProfile -count=1`. It checks nearest face/distance against the linear reference before timing, reports five paired timings, build costs, query allocations and separately collected traversal counters. Without these variables the real-scene profile test is skipped; synthetic regressions still run.
