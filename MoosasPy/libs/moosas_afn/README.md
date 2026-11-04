# MOOSAS airflow adapter

`MoosasAFN` converts MOOSAS network input into CONTAM projects and parses native
results. It is separate from the CONTAM installation in `../contam`.

Runtime executables are in `linux-x86_64/bin` and `windows-x86_64/bin`.
Source and the [build script](build.sh) live in this directory.

From the repository root, `bash MoosasPy/libs/moosas_afn/build.sh` installs both x86-64
targets here. Use `stage /absolute/output/directory` to build candidates using
the same platform/bin layout without replacing the packaged executables.
