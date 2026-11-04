# Annual daylight runtime

Pinned official **Radiance 6.0.2**, release `rad6R0P2`, source revision
`c1700d56cc`. Only the seven command-line executables used by daylight, their
four `.cal` files, and the Windows VC runtime dependency are included.
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
missing tools do not fall back to `libs/weather` or a system PATH installation.
An injected engine must provide the same environment for nested `rfluxmtx`
processes. Existing radiation/UHI tools remain separate.
