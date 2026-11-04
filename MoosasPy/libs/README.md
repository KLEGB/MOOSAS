# Packaged native runtimes

Each directory owns one native tool installation:

| Directory | Tool |
| --- | --- |
| `radiance` | Radiance daylight and cumulative-sky tools; retained epw2wea |
| `contam` | CONTAM solver, simread, and response template |
| `moosas_rad` | MOOSAS ray-intersection engine |
| `moosas_afn` | MOOSAS airflow network adapter |
| `moosas_energy` | MOOSAS surface-energy engine (CES3 v2) |

Executables are in `<tool>/<linux-x86_64|windows-x86_64>/bin`.
Radiance function files are in the adjacent platform `lib` directory.
Tool documentation and redistribution terms remain with the tool.

Go sources and build scripts live alongside the corresponding `moosas_rad`
and `moosas_afn` runtimes and ship with the package. Solar directions and the airflow
pressure model are in [`db`](../db). Tests and fixtures live under repository
`test`; simulation inputs and outputs belong to `SimulationWorkspace`.
