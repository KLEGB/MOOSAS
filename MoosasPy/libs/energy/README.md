# CETUS 能耗模块与热工模板

本文件统一说明能耗接口、CES3 v2 二进制协议、热工模板与验收方式。
代码及资源路径均相对于仓库根目录。

## 模块位置

| 路径 | 职责 |
| --- | --- |
| `MoosasPy/simulation/energy/input.py` | SI 输入数据类型、校验、模型几何与运行日程解析 |
| `MoosasPy/simulation/energy/runner.py` | CES3 编解码、持久 EXE 会话、预热、逐时运行、断点续算与结果汇总 |
| `MoosasPy/model/io/gbxml/` | gbXML 导入导出与热工来源信息 |
| `MoosasPy/model/thermal_templates.py` | 模型共享的模板读取、校验与应用 |
| `MoosasPy/model/energy_metadata.py` | RDF/XML/JSON 模型持久化共享的能耗元数据 |
| `MoosasPy/db/building_template.json` | 热工模板数据 |
| `MoosasPy/libs/energy/` | Windows/Linux 引擎可执行文件及本说明 |

公开接口继续从 `MoosasPy.simulation.energy` 导入。内部的 `requests.py`
并入 `input.py`，`protocol.py` 并入 `runner.py`；直接使用内部模块的调用方
需同步修改导入。gbXML 转换接口仍从 `MoosasPy.model.io.gbxml` 导入，
命令行入口仍为 `python -m MoosasPy.model.io.gbxml`。
通用 `simulation/engine.py` 提供进程执行注入，不包含 CETUS 协议逻辑。

## CETUS surface energy contract

`EnergyRunner` runs a persistent precompiled `CetusSurfaceEnergy --binary`
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

The default thermal library is `db/building_template.json`. Select a structured
profile with `template_id` and an optional `template_path`; the Python adapter
validates and resolves it before registering numeric models with CETUS. Use
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
diagnostics. CES3 v1 remains supported by CETUS for its existing callers.

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
Build CETUS explicitly with `python -m test.build_energy --moosas-dir <MoosasPy>`
in its repository. The codec in `MoosasPy/simulation/energy/runner.py` must preserve wire
compatibility with the reference `ENERGY/surface_v2.py`; file identity is not required.

From the MoosasPy repository run:

```
python -m pytest test/test_cetus_energy.py test/test_energy_engine.py -q
python test/accept_cetus_energy.py --hours 8760 --preheat 4
```

Acceptance outputs in `test/output/cetus_energy/` contain the source model path,
resolved boundaries/default sources, actual engine command, hourly typed numeric
arrays, checkpoint and annual summary. They are generated artifacts, not templates.

## 热工 JSON 模板（版本 1）

规范名称为 **GB 55015—2021《建筑节能与可再生能源利用通用规范》**，2022-04-01 实施。正式来源为[北京市政府公布的规范正文](https://www.beijing.gov.cn/zhengce/zhengcefagui/qtwj/202204/W020220412578195558564.pdf)。`building_template.json` 保存来源链接、正文 SHA-256、表号和适用条件。

### 结构和范围

`schema` 和 `version` 标识契约。`envelopes` 保存表格规则，`operations` 保存用途及参考运行条件，`assumptions` 集中保存工程假设，`templates` 引用前两者。

住宅对应表 3.1.8-1～11、3.1.9-1～5；甲类公共建筑对应表 3.1.10-1～6；乙类公共建筑对应表 3.1.11-1～2。共 55 个用途与围护组合。没有规范表格支持的公共建筑温和 B 区等组合不生成。乙类应按附录 B 的单体面积及建筑群面积条件另行选择，程序不从单个房间面积推断类别。隔墙及不采暖地下室上部楼板等限值作为扩展资料保留；当前相邻区域按绝热处理，不把这些限值用于区间传热。入口门及阳台栏板不属于本次适配范围。

`roof_u`、`wall_u`、`exposed_floor_u` 和 `window_u` 单位均为 W/(m²·K)。条件数组的两列由 `column` 定义：`storeys` 为不超过 3 层/超过 3 层，`shape_factor` 为不超过 0.30/超过 0.30，`inertia` 为 D 不超过 2.5/超过 2.5。窗数组先按 `wwr_bins` 选择行，再选择列。`null` 表示本表未规定，不表示零。

SHGC 是整体太阳得热系数限值，独立于传给 CETUS 的太阳透射率。窗的方向分列保留在 `shgc_directions` 中；外法线 +Y 为北，计算时应用 `EnergyDefaults.north_angle_degrees`。住宅窗墙比按房间同朝向合计，公共建筑按全模型同朝向合计，选择房间子集不会改变该比值。地下部分和绝热部分不计入外立面窗墙比。

模板资源入口为 JSON，模板名称即 `templates` 中的结构化名称。新建空间默认使用夏热冬冷 A 区住宅（`residential_hot_summer_cold_winter_A_GB55015-2021_RESIDENTIAL`），实际项目应显式选择适用模板。

### 使用接口

```python
from MoosasPy.model import apply_building_template, building_templates
from MoosasPy.simulation.energy import EnergyRunner

# 查看可选名称；JSON 可换成项目自己的同版本文件。
records = building_templates()
apply_building_template(
    model, "public_A_cold_GB55015-2021_OFFICE",
    context={"shape_factor": 0.28},
)
result = EnergyRunner(model, weather).run()

# 也可直接在运行时加载，既有模型及输入文件不被修改。
result = EnergyRunner(
    model, weather,
    template_path=r"E:\project\thermal_templates.json",
    template_id="public_A_cold_GB55015-2021_OFFICE",
    template_context={"shape_factor": 0.28},
    template_overrides_model=True,
).run()
```

`apply_building_template` 保存展开后的模板快照到空间，RDF/XML/模型 JSON 均可携带该快照；再次载入模型不依赖原来的自定义模板文件。该接口默认保留已有显式空间参数及构件参数。`preserve_explicit=False` 仅重置空间运行参数，构件仍保留。若要进行完整模板构件场景对比，使用运行参数 `template_overrides_model=True`；这也不修改模型。

默认优先级为运行/表面侧覆盖、显式模型参数、JSON 模板、通用默认假设。`template_overrides_model=True` 允许模板替代已有构件与空间值，但运行时覆盖仍优先。完整的 layers/R/U 视为一组，避免同时保留冲突的物理表示。可通过 `template_context` 提供 `storeys`、`shape_factor`、`thermal_inertia_D`、`window_wall_ratio`；后者是整次运行的显式比值覆盖。住宅层数默认由模型地面及以上空间的标高数估计，可显式修正。体形系数和热惰性指标未知时选择较严格的一列，程序不将热容量当作热惰性指标 D。

### 等效模型及假设

- 规范规定的是性能限值，不提供完整材料层。模板按限值建立等效构件；热容量、发射率、吸收率及接地基层热阻等来自 `assumptions`，并非规范规定值。墙/楼板热容量为完整构件值；相邻房间各分配半厚度，在中面绝热，保留室内蓄热。
- 接地表中的 R 是**保温材料层热阻下限**，不直接充当完整构件热阻。计算加入假设的基层 R；周边地面保温下限用于整个地板是已声明的简化场景。没有给定限值的构件使用单独的假设 R。
- 空气到空气 U 值扣除参考表面热阻后传给 CETUS，避免重复计入表面换热。等效 U 包含结构性热桥影响；模型不生成实际热桥节点。
- 夏季等效玻璃采用假设吸收率 0.1，`tau=max(SHGC上限-0.1,0)`，`rho=1-SHGC上限`，使 `tau+alpha` 不超过上限。该模型不代表具体玻璃产品，也不证明冬季 SHGC 下限成立。冬季要求单独记录并给出提示；真实项目可用产品光学参数及逐时遮阳辐照覆盖。
- 运行参数选取附录 C 的参考照明、设备、人员、新风和部分日程。JSON 中的日程是 0～1 系数，Python 侧显式乘以强度转为逐时绝对值；既有命名日程仍按绝对值解释。使用周一开始的典型周，未实现法定节假日、温控升降温过渡及住宅卧室/起居室加权。住宅采用起居室作为代表单区，冷热设定为 18/26℃。
- 人员显热 75 W/人、公共建筑背景渗透 0.5 ACH、热泵制热 COP 2.6 是工程场景假设。制冷 COP 使用参考折算值，不代表选型设备效率；严寒 A/B/C、寒冷 A 住宅统一采用 3.6 是扩展假设，规范表 C.0.7-2 的住宅 3.6 参考值适用范围须另行核对。住宅规范参考换气次数按寒冷地区 0.5、其他地区 1.0 ACH 取值，不重复添加人均新风。

解析结果中每个表面提供 `template_id`、`template_limits`、`template_context` 和逐字段 `sources`。前述限值是选中的规范目标，显式构件覆盖后不表示该构件满足限值。超出窗墙比、体形系数或屋顶透光比例适用范围会给出提示。模板不替代整栋建筑的规范符合性核算。

### 实际导入链路与验收

导入链路为 **JSON → MoosasPy 模板校验与参数解析 → CES3 v2 数值二进制 → CETUS Energy**。Go 引擎接收解析后的构件与运行参数，不直接读取这份模板库；没有恢复旧 i/o 协议。

```powershell
python -B -m pytest test/test_thermal_templates.py -q
python -B test/accept_cetus_energy.py --template public_A_cold_GB55015-2021_OFFICE --template-overrides-model --output-dir test/output/cetus_thermal_templates --hours 8760
```

验收脚本保存 JSON 库与引擎哈希、逐表面解析值、边界表、逐时能耗和最终状态。测试覆盖所有 55 个模板的实际二进制注册和计算、自定义 JSON 数值修改、运行覆盖、典型周强度换算，以及三种模型格式的往返。

## CETUS 1.9.4：建筑侧理想潜热负荷

本次以 `energyv135` 的 `eab365642545ad0d3b9da9f7f69d2d3beac2ef07` 为基线，
将已交付 CETUS 1.9.4 的水汽平衡接入现有 `input.py` / `runner.py`。
1.9.4 的原生显热 Go 内核与 1.9.3 相同，因此继续使用本目录既有的 CES3 v2
Windows/Linux 可执行文件，不重编译、不增加传输字段或新的引擎进程。

```python
model.energy_settings['latent'] = {'enabled': True}  # 缺省 false
space.settings['zone_popmoisture'] = 0.06  # kg/(h person)，或已有日程名
space.settings['zone_c_hum'] = 0.60       # 0–1，复用已有湿度设定参数
result = EnergyRunner(model=model, weather=weather, hours=24).run()
print(result.latent_load)
```

也可传 `EnergyRunner(..., latent={'enabled': True})`，覆盖模型的开关。
现有 `zone_overrides` 可以按热区覆盖上述参数；人员密度仍使用 `zone_ppsm`。
新默认值仅为人员产湿 0.06 kg/(h·人)、缺省 RH 上限 0.6，均为可编辑先验。
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

NBuilding 测试：`python -m test.run_cetus194_nbuilding`。
详见 `doc/CETUS_1.9.4_integration.md` 和 `test/test_cetus194.py`。
