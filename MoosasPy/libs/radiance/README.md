# Radiance runtime

Pinned official **Radiance 6.0.2**, release `rad6R0P2`, source revision
`c1700d56cc`. The seven command-line executables used by daylight and cumulative
sky, their four `.cal` files, and the Windows VC runtime dependency are included.
Original redistribution terms are in `LICENSE.txt`.

Source: https://github.com/LBNL-ETA/Radiance/releases/tag/rad6R0P2

Verified original archive SHA-256:

- Linux: `04ee53cafbb64b943a53616b3d0ee379dd7ef80379c83aa7a145e547d9809c28`
- Windows: `c4f02efcb4a33f4a43b7ce0ba58100544abc317a1be61f7b0425a64a66eb8a09`

Linux x86-64 execution is tested. Windows x86-64 execution is also covered by
the native annual, weather-PIT and analytic-reflection tests. Other
platforms may pass a compatible complete Radiance installation's `bin` as
`executable_dir`. Its adjacent `lib` must contain Radiance function files.

`RadianceRunner` sets `PATH` and `RAYPATH` on its own subprocess engine only.
Only the selected installation supplies executables and function files:
missing tools do not fall back to another directory or a system PATH installation.
An injected engine must provide the same environment for nested `rfluxmtx`
processes. Existing radiation/UHI tools remain separate.

`epw2wea` is retained unchanged from the previously bundled weather utilities;
it is not claimed to come from the pinned 6.0.2 archives above. Its original
release/archive provenance is not recorded in this repository. It remains a
separate EPW conversion tool; the strict annual daylight converter is Python.

The old/new `gendaymtx -m 1 -O1` output was compared using the same full Beijing
TMY WEA. Both RGB matrices had 1,278,960 rows and 3 components; normalized L1
difference was 0.1245121%. The version upgrade is not numerically bit-identical.
The existing cumulative-sky conversion, units and row selection are unchanged.
After that conversion the 145-by-8760 matrix differs by 0.1220346% normalized
L1; EPW-to-WEA output is byte-identical. These are fixture-specific regression
measurements, not a universal bound on other weather files.
