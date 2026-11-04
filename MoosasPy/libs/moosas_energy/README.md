# MOOSAS Energy 能耗模块与热工模板

本文件统一说明能耗接口、CES3 v2 二进制协议、热工模板与验收方式。
代码及资源路径均相对于仓库根目录。
`MoosasEnergy` 为原 CETUS 引擎的打包名称；本次仅重命名文件，
不改变二进制内容、CES3 协议或计算算法。

## 模块位置

| 路径 | 职责 |
| --- | --- |
| `MoosasPy/simulation/energy/input.py` | SI 输入数据类型、校验、模型几何与运行日程解析 |
| `MoosasPy/simulation/energy/runner.py` | CES3 编解码、持久 EXE 会话、预热、逐时运行、断点续算与结果汇总 |
| `MoosasPy/model/io/gbxml/` | gbXML 导入导出与热工来源信息 |
| `MoosasPy/model/thermal_templates.py` | 模型共享的模板读取、校验与应用 |
| `MoosasPy/model/energy_metadata.py` | RDF/XML/JSON 模型持久化共享的能耗元数据 |
| `MoosasPy/db/template/` | 按国家、标准版本和建筑类型组织的热工模板数据 |
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

The thermal catalog is `db/template/library.json`. No national standard or
template is selected by default. Select a namespaced profile with `template_id`
and an optional catalog-directory `template_path`; the Python adapter
validates and resolves it before registering numeric models with MOOSAS Energy. Use
`template_context` for known storeys, shape factor or inertia index.
`template_overrides_model=True` creates a template scenario without mutating the
model; runtime overrides still win. Each surface carries template ID, selected
limits and context as audit metadata, excluded from the native numeric schema.
See the thermal template section below for units, sources, assumptions and precedence.

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

## 热工 JSON 模板（版本 2）

现有中国标准包为 **GB 55015—2021《建筑节能与可再生能源利用通用规范》**，2022-04-01 实施。正式来源为[北京市政府公布的规范正文](https://www.beijing.gov.cn/zhengce/zhengcefagui/qtwj/202204/W020220412578195558564.pdf)。`db/template/CN/GB55015-2021/common.json` 保存来源链接、正文 SHA-256、表号和适用条件。这些来源和假设只属于该标准包，不作为其他国家的默认值。

### 结构和范围

`schema` 和 `version` 标识契约。入口 `library.json` 显式列出 `COUNTRY/STANDARD-EDITION` 标准包，不设置默认模板。每个包的 `common.json` 保存 `standard`、`envelopes`、`assumptions`、`unit_system: SI`、`projection` 和 `building_types` 文件列表；类型文件保存 `operation_id`、`operation`、适用 `envelopes`、逐围护组合的 `operation_overrides` 和 `wwr_scope`。模板 ID 为 `COUNTRY/STANDARD-EDITION/ENVELOPE/BUILDING-TYPE`。

新增其他国家标准时，在独立目录编写该标准的数据并注册到入口列表，不修改中国标准包。不同标准、不同版本可使用相同局部名称而不冲突。数据须先归一为支持的 SI 投影契约 `prescriptive-envelope-v1`；尚不支持的投影、规则、单位、未知引用及重复命名均报错，不回落中国标准。本轮未引入未经核验的国外规范数据。

住宅对应表 3.1.8-1～11、3.1.9-1～5；甲类公共建筑对应表 3.1.10-1～6；乙类公共建筑对应表 3.1.11-1～2。共 55 个用途与围护组合。没有规范表格支持的公共建筑温和 B 区等组合不生成。乙类应按附录 B 的单体面积及建筑群面积条件另行选择，程序不从单个房间面积推断类别。隔墙及不采暖地下室上部楼板等限值作为扩展资料保留；当前相邻区域按绝热处理，不把这些限值用于区间传热。入口门及阳台栏板不属于本次适配范围。

`roof_u`、`wall_u`、`exposed_floor_u` 和 `window_u` 单位均为 W/(m²·K)。条件数组的两列由 `column_selector.parameter` 和 `threshold` 定义，分别表示不超过/超过阈值。中国包声明的层数阈值为 3、体形系数为 0.30、热惰性指标为 2.5，这些值不写死在计算层。窗数组先按 `wwr_bins` 选择行，再选择列。`null` 表示本表未规定，不表示零。

SHGC 是整体太阳得热系数限值，独立于传给 MOOSAS Energy 的太阳透射率。方向分列由 `shgc_sectors` 声明起止方位角、端点包含关系、列索引和冬季限值适用性，未命中扇区使用显式的 `shgc_default_index`；外法线 +Y 为北，计算时应用 `EnergyDefaults.north_angle_degrees`。`wwr_scope` 控制按房间还是全模型同朝向合计，中国包的住宅用 `zone`、公建用 `building`，不根据建筑用途在代码中推断。选择房间子集不会改变全模型比值；地下部分和绝热部分不计入外立面窗墙比。

新建空间不自动选择任何国家标准或住宅模板，必须显式指定完整模板 ID。未选模板时，源模型参数和通用工程假设不代表任何国家的规范符合性。

### 使用接口

```python
from MoosasPy.model import apply_building_template, building_templates
from MoosasPy.simulation.energy import EnergyRunner

# 查看可选名称；可用同版本目录格式的自定义目录替换 catalog。
records = building_templates()
apply_building_template(
    model, "CN/GB55015-2021/public_A_cold/OFFICE",
    context={"shape_factor": 0.28},
)
result = EnergyRunner(model, weather).run()

# 也可直接在运行时加载，既有模型及输入文件不被修改。
result = EnergyRunner(
    model, weather,
    template_path=r"E:\project\template",
    template_id="CN/GB55015-2021/public_A_cold/OFFICE",
    template_context={"shape_factor": 0.28},
    template_overrides_model=True,
).run()
```

`apply_building_template` 保存展开后的模板快照到空间，RDF/XML/模型 JSON 均可携带该快照；再次载入模型不依赖原来的自定义模板文件。该接口默认保留已有显式空间参数及构件参数。`preserve_explicit=False` 仅重置空间运行参数，构件仍保留。若要进行完整模板构件场景对比，使用运行参数 `template_overrides_model=True`；这也不修改模型。

版本 1 文件、旧路径、非命名空间模板 ID 和旧快照不提供兼容分支。旧快照载入或求解时明确报错；需从源几何重建并显式应用版本 2 模板。`space.applySettings()` 的字符串参数也必须精确匹配完整 ID，不再使用正则模糊匹配。

默认优先级为运行/表面侧覆盖、显式模型参数、JSON 模板、通用默认假设。`template_overrides_model=True` 允许模板替代已有构件与空间值，但运行时覆盖仍优先。完整的 layers/R/U 视为一组，避免同时保留冲突的物理表示。可通过 `template_context` 提供 `storeys`、`shape_factor`、`thermal_inertia_D`、`window_wall_ratio`；后者是整次运行的显式比值覆盖。住宅层数默认由模型地面及以上空间的标高数估计，可显式修正。体形系数和热惰性指标未知时选择较严格的一列，程序不将热容量当作热惰性指标 D。

### 等效模型及假设

以下具体数值描述现有中国标准包；其他标准包应声明自己的工程假设。

- 规范规定的是性能限值，不提供完整材料层。模板按限值建立等效构件；热容量、发射率、吸收率及接地基层热阻等来自 `assumptions`，并非规范规定值。墙/楼板热容量为完整构件值；相邻房间各分配半厚度，在中面绝热，保留室内蓄热。
- 接地表中的 R 是**保温材料层热阻下限**，不直接充当完整构件热阻。计算加入假设的基层 R；周边地面保温下限用于整个地板是已声明的简化场景。没有给定限值的构件使用单独的假设 R。
- 空气到空气 U 值扣除参考表面热阻后传给 MOOSAS Energy，避免重复计入表面换热。等效 U 包含结构性热桥影响；模型不生成实际热桥节点。
- 夏季等效玻璃采用假设吸收率 0.1，`tau=max(SHGC上限-0.1,0)`，`rho=1-SHGC上限`，使 `tau+alpha` 不超过上限。该模型不代表具体玻璃产品，也不证明冬季 SHGC 下限成立。冬季要求单独记录并给出提示；真实项目可用产品光学参数及逐时遮阳辐照覆盖。
- 中国包运行参数选取附录 C 的参考照明、设备、人员、新风和部分日程。JSON 中的日程是 0～1 系数，Python 侧显式乘以强度转为逐时绝对值；既有命名日程仍按绝对值解释。`operation.week` 用七个日型引用声明周一至周日的安排，不假设各国周末相同。中国包仍使用五个工作日、两个休息日，未实现法定节假日、温控升降温过渡及住宅卧室/起居室加权。住宅采用起居室作为代表单区，冷热设定为 18/26℃。
- 人员显热 75 W/人、公共建筑背景渗透 0.5 ACH、热泵制热 COP 2.6 是工程场景假设。制冷 COP 使用参考折算值，不代表选型设备效率；严寒 A/B/C、寒冷 A 住宅统一采用 3.6 是扩展假设，规范表 C.0.7-2 的住宅 3.6 参考值适用范围须另行核对。住宅规范参考换气次数按寒冷地区 0.5、其他地区 1.0 ACH 取值，不重复添加人均新风。

解析结果中每个表面提供 `template_id`、`template_limits`、`template_context` 和逐字段 `sources`。前述限值是选中的规范目标，显式构件覆盖后不表示该构件满足限值。超出窗墙比、体形系数或屋顶透光比例适用范围会给出提示。模板不替代整栋建筑的规范符合性核算。

### 实际导入链路与验收

导入链路为 **JSON → MoosasPy 模板校验与参数解析 → CES3 v2 数值二进制 → MOOSAS Energy**。Go 引擎接收解析后的构件与运行参数，不直接读取这份模板库；没有恢复旧 i/o 协议。

```powershell
D:\miniconda3\Scripts\conda.exe run -n moosas python -B -m pytest test\test_example_integration.py -k energy_engine -q
```

能耗集成测试显式选择中国住宅模板检查全年计算，不等同于对所有 55 个模板逐一执行二进制验收。
