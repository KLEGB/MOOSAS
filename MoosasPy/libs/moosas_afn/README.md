# MOOSAS airflow adapter

`MoosasAFN` converts MOOSAS network input into CONTAM projects and parses native
results. It is separate from the CONTAM installation in `../contam`.

Runtime executables are in `linux-x86_64/bin` and `windows-x86_64/bin`.
Source and build script are in [`native/moosas_afn`](../../../native/moosas_afn).

From the repository root, `bash native/moosas_afn/build.sh` installs both x86-64
targets here. Use `stage /absolute/output/directory` to build candidates using
the same platform/bin layout without replacing the packaged executables.
