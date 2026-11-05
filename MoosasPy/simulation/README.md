# Simulation API Conventions

Public simulation APIs follow these rules. When a public API is replaced,
remove the legacy interface in the same change without a transition period.

- Use `snake_case` for modules, functions, parameters, local variables, and
  result fields.
- Use `PascalCase` for classes and dataclasses. Suffix input dataclasses with
  `Request` and returned dataclasses with `Result`.
- Use `run_<domain>` for one-shot public functions and `<Domain>Runner.run()`
  for runner APIs. Use `build_<artifact>` and `parse_<artifact>` for input and
  output conversions.
- Preserve unit abbreviations that are established domain terms, such as `idf`,
  `epw`, `afn`, and `ach`; write new compound names as `idf_path`, `epw_file`,
  and `ach_values`.
- Every runner returns a subclass of `SimulationResult`. Domain calculations
  belong in explicit result fields; external-process diagnostics go in
  `commands`; non-fatal conditions go in `warnings`; workspace diagnostics go
  in `workspace`.
- Results are frozen dataclasses. Collections exposed by results are tuples,
  so a completed simulation result remains reproducible and inspectable.
- Native processes are invoked through the `NativeEngine` protocol. The local
  default is `SubprocessEngine`; tests and alternative providers should inject
  an engine rather than patch domain logic.
- Run files are owned by `SimulationWorkspace`. Short-lived calculations use
  auto-cleaned workspaces. Workflows that need follow-up files explicitly use
  retained workspaces and expose their location through `WorkspaceReport`.

PIT Radiance and airflow runs retain their workspace when `work_dir` is
provided; runs without it use auto-cleaned temporary directories.

`convert_epw_to_wea(epw_file, output_path)` is the single local EPW-to-WEA
converter for energy weather preparation and annual daylight. It preserves
coordinate precision and fractional UTC offsets and validates a non-leap
8760-hour time axis and DNI/DHI. The former `prepare_daylight_weather` entry
point and the converter's native-process timeout argument are removed.

Cumulative sky matrices exclude gendaymtx's ground row and retain all 145 sky
patches. This correction can change radiation and downstream energy results.

The scoped energyv135 integration does not enable mechanical ACH, adapter
wind paths, regional thermal boundary overrides, or thermal surface aggregation.
Building parameter templates remain unchanged.
