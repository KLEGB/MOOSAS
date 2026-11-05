# Simulation Conventions

Native Space setting `zone_mechanical_ach` is an explicitly assigned outdoor
makeup-equivalent mechanical ventilation rate in 1/h. The energy adapter adds
it to occupied/unoccupied infiltration ACH, then adds per-person outdoor air
from `zone_pfav` (m3/h/person). Its default is zero for existing models. Supply
or exhaust design capacities must not be assigned as continuous outdoor air
without recording the operating and makeup-air assumption. The optional CETUS 1.9.4 latent-load diagnostic uses these same outdoor
airflows and existing hvac_enabled schedule; it does not model an HVAC duct network.

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
