# CETUS surface energy contract

`EnergyRunner` runs a persistent precompiled `CetusSurfaceEnergy --binary`
process. Inputs are `EnergyRequest`, not legacy CSV rows or CLI argument arrays.
Every selected space is an independent single thermal zone. Conductive exchange
between zones is adiabatic; inter-zone airflow is not part of this calculation.

## Model and overrides

`build_energy_input(model, weather, ...)` resolves individual surface sides.
The same floor slab has a `floor` side in the room above and a `ceiling` side in
the room below. Source Uids and owner lists, including owners outside a selected
subset, determine shared boundaries; a stale `isOuter=True` never overrides a
known shared boundary. Net opaque areas exclude the union of projected openings.
Role and boundary are independent: a floor can be outdoors, ground, or adiabatic.

Overrides may be saved on `model.energy_settings`:

```python
model.energy_settings = {
    "ground_level_m": 0.0,
    "surface_overrides": {
        "wall_0_42": {
            "boundary_condition": "adiabatic",
            "side_overrides": {"2F215": {"inside_emissivity": 0.9}}
        }
    },
    "zone_overrides": {"2F215": {"zone_h_temp": 20, "zone_c_temp": 26}}
}
```

The same dictionaries can be supplied as run arguments. A complete surface-side
ID (`zone_id/element_uid/role`) can also be used as an override key. Values on
element `settings` remain supported. Explicit run/side/element values take
precedence over mapped zone parameters and `EnergyDefaults`. Each resolved
surface records the source of its parameters in `sources`.

The default thermal library is `db/building_template.json`. Select a structured
profile with `template_id` and an optional `template_path`; the Python adapter
validates and resolves it before registering numeric models with CETUS. Use
`template_context` for known storeys, shape factor or inertia index.
`template_overrides_model=True` creates a template scenario without mutating the
model; runtime overrides still win. Each surface carries template ID, selected
limits and context as audit metadata, excluded from the native numeric schema.
See `db/THERMAL_TEMPLATES.md` for units, sources, assumptions and precedence.

Construction resistance is in m² K/W, areal capacity in J/(m² K). Full-element
layers follow the source element normal, from its back to its front. The adapter
orients each room's layers from the back boundary towards the room. Explicit
`construction_scope="surface_side"` layers already use this latter order.
Physical layers accept
`thickness_m`, `conductivity_w_mk`, `density_kg_m3`, `specific_heat_j_kgk`; reduced
layers accept `resistance_m2k_w`, `capacity_j_m2k`, and optional `thickness_m`.
Layer capacities replace a lumped capacity. An air-to-air U value is converted
once using declared reference films (`u_convention`, `reference_inside_m2k_w`,
`reference_outside_m2k_w`). Supply construction R directly when this convention
does not describe the source U value.

Shared opaque constructions are cut at the thickness midplane, retaining the
half adjacent to each room. When layer thicknesses are unavailable the cut uses
equivalent resistance. Set `construction_scope="surface_side"` if an override
already describes one side, to prevent a second split. Two exposed sides do not
duplicate total physical capacity. Glazing is massless.

Adiabatic back faces have zero exterior convection, radiation and ground contact,
while retaining indoor convection/radiation and storage. Ground boundaries only
exchange through their contact coefficient. Missing outdoor film values select
the hourly `5.8 + 4.1 * wind_speed` model; an explicit zero disables convection.
Inside and outside emissivity are
independent. Solar transmittance, visible transmittance and SHGC are distinct;
ambiguous legacy `transparency`/`reflection` values are not silently reinterpreted.

## Time, weather and results

Weather is hourly and non-leap (8760 records); dt is 3600 s. MOOSAS sky temperature
is Kelvin, converted to sky irradiance as sigma*T^4. Existing `.sch` records carry
absolute values; no design intensity is multiplied into those values. Daily,
weekly (day zero Monday), and explicit hourly schedules are supported.

By default, incident solar is an unshaded direct/diffuse projection from hourly
GHI/DHI, using declared timezone/north angle and inferred direct normal radiation.
This assumption is recorded in result warnings. Supply `irradiance_by_surface`
(surface-side ID or element Uid -> 8760 incident W/m² values) for computed or
measured boundaries. `require_radiation=True` requires explicit hourly boundaries;
seasonal space gains are not used. Ground and adiabatic external solar is zero.

HVAC is ideal sensible heating/cooling with constant zone COP. `conditioned=False`
or a zero `hvac_enabled` schedule gives free-running temperatures. Native HVAC W
is positive for heating, negative for cooling. Electricity W is HVAC only; the
adapter separately accounts for direct lighting/equipment electricity. Existing
equipment/lighting heat-density schedules assume the same electrical input and
fully sensible heat release. Powers are integrated before area normalization.

`EnergyResult.zone_hourly_energy_kwh` has shape (zones, simulated hours, 4), in
cooling/heating/lighting/equipment order. `data` derives unrounded kWh/m² totals,
monthly/daily/hourly views; building intensity is area weighted. Raw temperatures
and state remain explicit result fields. `surface_results=True` retains hourly
surface diagnostics. This can consume substantial memory for annual cases.

Default warmup repeats the first 24 forcing hours for at least four cycles,
until the maximum daily state change is <=0.1 K, capped at 120 cycles. Warmup is
excluded from energy totals. A checkpoint binds complete state to the model and
program signature, zone ordering and next HOY. Restart with `initial_state` and
contiguous `start_hour`; no warmup is applied. The model/domain metadata round
trips through RDF, XML and JSON, including nested surface overrides and layers.

## CES3 v2 wire layout

Frame header: little-endian `<4sHHQQ>` = `CES3`, version 2, opcode, request ID,
payload length. Limit: 256 MiB. Strings and f64 vectors start with u32 counts.
Opcode 0 returns three u32 values: schema=2, state layout=1, boundary mask=7.
Opcode 1 registers models without forcing or initial-state records:

```
u32 model_count
each model:
  string zone_id
  f64[6] dt_s, initial_c, cooling_cop, heating_cop,
         internal_radiant_fraction, solar_floor_fraction
  u32 surface_count
  each surface:
    string name, source_element_id, role
    u32 boundary: outdoors=0, ground=1, adiabatic=2
    f64[13] area, construction_R, lumped_capacity, solar_reflectance,
            solar_transmittance, outside_emissivity, hi, ho, sky_view,
            ground_view, ground_contact, absorbed_inside_fraction,
            inside_emissivity
    u32 layer_count
    each layer: f64 R, capacity
```

Opcode 2 begins a step: u64 step ID, u32 zone count, then each zone's state vector,
9 forcing scalars and 3 vectors in `protocol.py` order, followed by u32 HVAC flag.
Opcode 7 evaluates the immutable old state. The `<QIIII>` request selects step,
response level, return-state flag, aggregation, and forcing mask. Levels 0/1/2
retain CES3 coupling semantics; level 3 returns the 13 thermal summary fields
without surface detail. Opcode 4 ends a step. Error opcode 65535 carries UTF-8
diagnostics. CES3 v1 remains supported by CETUS for its existing callers.

## Skill source-alignment extensions

The bundled Skill retains the upstream numeric codec and engine unchanged.
`SurfaceRequest.radiation_geometry` carries world-space surface-side polygons
for MoosasRad; it is excluded from the native model and wire protocol.
`EnergyRunner(on_hour=callback)` optionally streams full native rows after each
simulation hour without retaining all surface diagnostics in memory.
External gbXML source evidence is persisted in model energy metadata and checked
before input resolution. Source area/volume, layers, controls and schedules take
precedence; conflicting overrides and unsupported source fields are rejected.

## Running and acceptance

```python
from MoosasPy.model import MoosasModel
from MoosasPy.simulation.weather import load_epw
from MoosasPy.simulation.energy import EnergyRunner

model = MoosasModel.load("building.rdf")
weather = load_epw("weather.epw", "analysis/weather")
result = EnergyRunner(model=model, weather=weather,
                      zone_ids=("2F215",), hours=24).run()
print(result.data["total"])
```

The bundled engine is selected by OS. `executable_path` or
`MOOSAS_ENERGY_EXECUTABLE` can select another prebuilt CES3 v2 executable.
Build CETUS explicitly with `python -m test.build_energy --moosas-dir <MoosasPy>`
in its repository. The reference codec `ENERGY/surface_v2.py` and vendored
`MoosasPy/simulation/energy/protocol.py` must remain identical.

From the MoosasPy repository run:

```
python -m pytest test/test_cetus_energy.py test/test_energy_engine.py -q
python test/accept_cetus_energy.py --hours 8760 --preheat 4
```

Acceptance outputs in `test/output/cetus_energy/` contain the source model path,
resolved boundaries/default sources, actual engine command, hourly typed numeric
arrays, checkpoint and annual summary. They are generated artifacts, not templates.
