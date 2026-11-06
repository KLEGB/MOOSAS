# MOOSAS Energy 能耗模块与建筑模板

本文件统一说明能耗接口、CES3 v2 二进制协议、建筑模板与验收方式。
代码及资源路径均相对于仓库根目录。
`MoosasEnergy` 为原 CETUS 引擎的打包名称；本次仅重命名文件，
不改变二进制内容、CES3 协议或计算算法。

## 模块位置

| 路径 | 职责 |
| --- | --- |
| `MoosasPy/simulation/energy/input.py` | SI 输入数据类型、校验、模型几何与运行日程解析 |
| `MoosasPy/simulation/energy/runner.py` | CES3 编解码、持久 EXE 会话、预热、逐时运行、断点续算与结果汇总 |
| `MoosasPy/model/io/gbxml/` | gbXML 导入导出与热工来源信息 |
| `MoosasPy/model/building_templates.py` | 建筑模板目录、校验及运行时适配 |
| `MoosasPy/model/metadata.py` | RDF/XML/JSON 模型持久化共享的模型元数据 |
| [MOOSAS-BuildingTemplate](https://github.com/KLEGB/MOOSAS-BuildingTemplate) | 独立私有模板库，按固定提交自动拉取到用户缓存 |
| `MoosasPy/libs/moosas_energy/` | Windows/Linux 引擎可执行文件（平台目录下的 `bin/`）及本说明 |

公开接口继续从 `MoosasPy.simulation.energy` 导入。内部的 `requests.py`
并入 `input.py`，`protocol.py` 并入 `runner.py`；直接使用内部模块的调用方
需同步修改导入。gbXML 转换接口仍从 `MoosasPy.model.io.gbxml` 导入，
命令行入口仍为 `python -m MoosasPy.model.io.gbxml`。
通用 `simulation/engine.py` 提供进程执行注入，不包含能耗协议逻辑。

## MOOSAS Energy surface energy contract

`EnergyRunner` runs a persistent precompiled `MoosasEnergy --binary`
process. Inputs are `EnergyRequest`, not legacy CSV rows or CLI argument arrays.
Every selected space is an independent single thermal zone. Conductive exchange
between zones is adiabatic; inter-zone airflow is not part of this calculation.

### Model and overrides

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

The building-template catalog is maintained in the private
[MOOSAS-BuildingTemplate repository](https://github.com/KLEGB/MOOSAS-BuildingTemplate).
On first default-catalog use, Git checks out a pinned commit using existing
credentials with repository read access; the validated checkout is cached in
`~/.cache/moosas/building-templates/<commit>/` for offline reuse.
Fetching is non-interactive and does not store credentials. The old bundled
`db/template/` path is removed. Its sole default runtime
standard is GB 55015-2021, with IDs such as
`CN/GB55015-2021/3A/Residential`. Select a profile explicitly with
`template_id`; an optional catalog-directory `template_path` can provide a
separate catalog. The Python adapter validates and resolves it before
registering numeric models with MOOSAS Energy. Templates contain direct,
climate/type-specific values; table-driven templates and contextual rule
selection are not supported.
`template_overrides_model=True` creates a template scenario without mutating the
model; runtime overrides still win. Each surface carries its template ID as
audit metadata, excluded from the native numeric schema.
See the building template section below for units, sources, assumptions and precedence.

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

### Time, weather and results

Weather is hourly and non-leap (8760 records); dt is 3600 s. MOOSAS sky temperature
is Kelvin, converted to sky irradiance as sigma*T^4. Model schedule records carry
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

### CES3 v2 wire layout

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
9 forcing scalars and 3 vectors in `simulation/energy/runner.py` (`HOUR_FIELDS`, `HOUR_ARRAYS`) order, followed by u32 HVAC flag.
Opcode 7 evaluates the immutable old state. The `<QIIII>` request selects step,
response level, return-state flag, aggregation, and forcing mask. Levels 0/1/2
retain CES3 coupling semantics; level 3 returns the 13 thermal summary fields
without surface detail. Opcode 4 ends a step. Error opcode 65535 carries UTF-8
diagnostics. The bundled engine retains upstream CES3 v1 support; this Python
adapter uses CES3 v2 only.

### Source-alignment extensions

The adapter preserves the upstream numeric wire layout and bundled engine.
`SurfaceRequest.radiation_geometry` carries world-space surface-side polygons
for MoosasRad; it is excluded from the native model and wire protocol.
`EnergyRunner(on_hour=callback)` optionally streams full native rows after each
simulation hour without retaining all surface diagnostics in memory.
External gbXML source evidence is persisted in model energy metadata and checked
before input resolution. Source area/volume, layers, controls and schedules take
precedence; conflicting overrides and unsupported source fields are rejected.

### Running and acceptance

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
The upstream engine build command is `python -m test.build_energy --moosas-dir <MoosasPy>`
in its source repository, not this repository. Deploy its output as
`MoosasEnergy` (`MoosasEnergy.exe` on Windows). The codec in
`MoosasPy/simulation/energy/runner.py` must preserve wire
compatibility with the reference `ENERGY/surface_v2.py`; file identity is not required.

From the MoosasPy repository run:

```
python -m pytest test/test_native_resources.py -q
python -m pytest test/test_example_integration.py -k energy_engine -q
```

The energy integration test checks the packaged executable against the full-year
geometry fixture and validates all 70 zones, monthly totals and reference loads.
Simulation outputs belong in caller-owned workspaces, not this tool directory.

## 建筑 JSON 模板（版本 4）

默认独立模板库仅注册 GB 55015—2021 标准，提供 99 个模板，覆盖 11 个气候区和 9 种建筑类型。首次加载需要 Git、联网及已有仓库读取凭据，成功后使用用户缓存；数据不再随包分发。运行时 ID 为 `CN/GB55015-2021/<climate-zone>/<building-type>`，例如 `CN/GB55015-2021/3A/Residential`。加载时校验标准元数据、文件名、参数和 schedule 引用，并将 schedule 展开为 8760 个值。

### 结构和范围

每个模板的 `BuildingTemplate` 包含 `Space`（`air_exchange`、`internal_gains`、`system`）和按构造分组的 `Envelope`（`exterior_wall`、`interior_wall`、`floor`、`ground`、`roof`、`window`、`skylight`）。空间 schedule 及围护结构的 U/SHGC 接入 Energy 适配器；可见光属性不作为太阳光学量使用。构造级 `air_leakage_per_area` 和 `opening_ratio` 保留于模板快照，目前 AFN/Radiance 求解器尚不消费这些字段。

模板参数使用统一值来源对象，逐个字段选择单值或外部 schedule：

```json
{"mode": "constant", "value": 30, "unit": "m3/h/person"}
{"mode": "schedule", "file": "schedules/office.sch",
 "name": "fresh_air", "unit": "m3/h/person"}
```

`.sch` 是 JSON 文件，包含具名且带单位的 8760 小时数组。文件路径相对模板包目录，禁止绝对路径和越界引用；参数与 schedule 单位必须一致。加载模板时会展开并校验 schedule，随建筑模板快照一并持久化，因此模型重新载入不依赖原 `.sch` 文件。Energy 不支持围护结构的动态 U/SHGC，若指定此类 schedule 会明确报错。

模板数据保留 `visible_reflectance`、`visible_transmittance`、`air_leakage_per_area` 和 `opening_ratio`，但目前它们不改变 Radiance/AFN 模拟。`air_leakage_per_area` 是模板场景参数，不应与空间渗透 ACH 混淆。

围护结构按构造组保存直接的 SI 数值：U 值使用 W/(m²·K)，SHGC 使用无量纲值。Energy 将这些直接输入映射到等效表面属性；模板不执行表格选列、方位适用性或 WWR 合规判断。

新建空间不自动选择住宅/建筑模板，必须显式指定完整模板 ID。未选模板时，源模型参数和通用工程假设不代表任何国家的规范符合性。

### 使用接口

```python
from MoosasPy.model import apply_building_template, building_templates
from MoosasPy.simulation.energy import EnergyRunner

# 查看可选名称。
records = building_templates()
apply_building_template(
    model, "CN/GB55015-2021/3A/Residential",
)
result = EnergyRunner(model, weather).run()

# 也可直接在运行时加载，既有模型及输入文件不被修改。
result = EnergyRunner(
    model, weather,
    template_id="CN/GB55015-2021/3A/Residential",
    template_overrides_model=True,
).run()
```

`apply_building_template` 保存展开后的模板快照到空间，RDF/XML/模型 JSON 均可携带该快照；再次载入模型不依赖原来的自定义模板文件。该接口默认保留已有显式空间参数及构件参数。`preserve_explicit=False` 仅重置空间运行参数，构件仍保留。若要进行完整模板构件场景对比，使用运行参数 `template_overrides_model=True`；这也不修改模型。

版本 1/2/3 模板文件、旧路径、非命名空间模板 ID 和旧快照不提供兼容分支。旧快照载入或求解时明确报错；需按版本 4 结构显式升级模板后重新应用。`space.applySettings()` 的字符串参数也必须精确匹配完整 ID，不再使用正则模糊匹配。

默认优先级为运行/表面侧覆盖、显式模型参数、JSON 模板、通用默认假设。`template_overrides_model=True` 允许模板替代已有构件与空间值，但运行时覆盖仍优先。完整的 layers/R/U 视为一组，避免同时保留冲突的物理表示。catalog 仅接受直接建筑模板，不支持表驱动规则或模板上下文。

### 等效模型及假设

以下说明区分直接模板值与 Energy 所需的等效模型假设；二者都不构成规范符合性认证或产品数据。

- GB values are direct simulation inputs, not compliance limits. Energy still uses declared package assumptions for construction properties not present in the records and for mapping SHGC to equivalent solar optics.
- Ground-contact construction without a direct U value uses its declared engineering resistance.
- SHGC 与 MOOSAS Energy 使用的太阳透射率不同。适配器使用模板包声明的玻璃吸收率映射等效太阳光学参数；不表示具体产品，也不替代逐时遮阳或规范符合性核算。
- 逐时参数应直接引用具有 8760 个值的 `.sch`；模板不隐含工作日、周末、节假日或工作时段。材料、系统效率、新风、渗透及设定点均由所加载的模板或显式模型设置提供。

解析结果中每个表面提供 `template_id` 和逐字段 `sources`。模板记录所选构造输入，但不替代整栋建筑的规范符合性核算。

### 实际导入链路与验收

导入链路为 **JSON → MoosasPy 模板校验与参数解析 → CES3 v2 数值二进制 → MOOSAS Energy**。Go 引擎接收解析后的构件与运行参数，不直接读取这份模板库；没有恢复旧 i/o 协议。

默认 catalog tests cover template selection and input mapping. Slow native-engine integration tests additionally require the packaged Energy executable and the Beijing weather fixture.

## 建筑侧理想潜热负荷（可选）

潜热水汽平衡在 Python 侧的 `input.py` / `runner.py` 中求解，
显热 Go 内核与 CES3 v2 Windows/Linux 可执行文件不变，
不增加传输字段或新的引擎进程。

```python
model.energy_settings['latent'] = {'enabled': True}  # 缺省 false
space.settings['zone_popmoisture'] = 0.06  # kg/(h person)，或已有日程名
space.settings['zone_c_hum'] = 0.60       # 0–1，复用已有湿度设定参数
result = EnergyRunner(model=model, weather=weather, hours=24).run()
print(result.latent_load)
```

也可传 `EnergyRunner(..., latent={'enabled': True})`，覆盖模型的开关。
现有 `zone_overrides` 可以按热区覆盖上述参数；人员密度仍使用 `zone_ppsm`。
默认值仅为人员产湿 0.06 kg/(h·人)、缺省 RH 上限 0.6，均为可编辑先验。
不增加 RDF 类、谓词体系或 element 子类，使用已有模型/空间 settings 及
energy metadata 保存、恢复参数与日程。新参数作用于空间，不添加到墙窗上。

每小时在显热解之后计算：
`G + m_f*(w_out-w_zone) + m_i*(w_out-w_zone) - D = 0`。
新风包括既有按人数新风以及显式 `zone_mechanical_ach` 的室外补风等效量；
渗风/夜间通风沿用既有占用时段选择。两部分流量之和严格等于显热输入流量。
干燥外气的负湿负荷先抵消人员产湿，再裁剪净除湿需求。显热内扰不得重复包含人员潜热。

湿度控制沿用现有 `hvac_enabled` 日程。启用时，即使没有显热制冷仍可产生潜热冷负荷；
关闭时没有除湿，返回自由湿平衡。零交换且有产湿的无控热区没有稳态解，湿度数组为 NaN，
回调中为 None，并输出 warning；自由数学解过饱和也记录 warning，不能作为有效物理湿度使用。
不引入蓄湿、材料吸放湿、实际盘管、加湿、再热或潜热电耗，也不把潜热加入耦合热流。

结果扩展在现有 `EnergyResult` 中：

- `zone_latent_cooling_w`、`zone_sensible_cooling_w`：热区×小时的热负荷 W；二者相加为总冷负荷。
- `zone_humidity_ratio`：kg/kg 干空气；`zone_relative_humidity`：0–1（无控超饱和数学解会超过 1）。
- `zone_dehumidification_kg_s`：理想除湿质量负荷。
- `latent_load` / `data['latent_load']`：显热/潜热/总冷负荷 kWh、除湿 kg、最大湿量残差。
- `on_hour` 回调每个热区行的 `latent`：实际湿边界、湿源/流量分项、目标/自由/求解湿度和残差。

原有 `zone_hourly_energy_kwh`、`load`、`data['total']` 继续为现有显热空调电耗、照明与设备电耗，
不能把新增热负荷直接解释为电耗。关闭开关时不分配湿度结果数组。湿度不增加热预热/断点状态，
现有热检查点可用于切换诊断开关；请求保留解析后的湿源日程与气象作为输入依据。

天气接口明确为：DeST CSV 第五列 g/kg；`WeatherData.humidity_ratio` kg/kg。
EPW 转换改用露点及实际气压计算含湿量，避免原经验多项式在低温时产生负值。
CSV 文件格式保持 g/kg，进入 WeatherData 时除以 1000；直接构造 WeatherData 应提供 kg/kg。

潜热解算器单元测试见 `test/test_simulation_contracts.py`，
天气单位换算见 `test/test_weather_architecture.py`，
RDF 往返见 `test/test_io_contract.py`，
NBuilding 开/关潜热等价性（slow）见 `test/test_example_integration.py`。
