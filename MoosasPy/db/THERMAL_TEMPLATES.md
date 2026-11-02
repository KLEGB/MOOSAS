# 热工 JSON 模板（版本 1）

规范名称为 **GB 55015—2021《建筑节能与可再生能源利用通用规范》**，2022-04-01 实施。正式来源为[北京市政府公布的规范正文](https://www.beijing.gov.cn/zhengce/zhengcefagui/qtwj/202204/W020220412578195558564.pdf)。`building_template.json` 保存来源链接、正文 SHA-256、表号和适用条件。

## 结构和范围

`schema` 和 `version` 标识契约。`envelopes` 保存表格规则，`operations` 保存用途及参考运行条件，`assumptions` 集中保存工程假设，`templates` 引用前两者，`aliases` 兼容旧名称，`legacy_templates` 单独保留未经本次复核的 GB/T 51350 旧记录。

住宅对应表 3.1.8-1～11、3.1.9-1～5；甲类公共建筑对应表 3.1.10-1～6；乙类公共建筑对应表 3.1.11-1～2。共 55 个用途与围护组合。没有规范表格支持的公共建筑温和 B 区等组合不生成。乙类应按附录 B 的单体面积及建筑群面积条件另行选择，程序不从单个房间面积推断类别。隔墙及不采暖地下室上部楼板等限值作为扩展资料保留；当前相邻区域按绝热处理，不把这些限值用于区间传热。入口门及阳台栏板不属于本次适配范围。

`roof_u`、`wall_u`、`exposed_floor_u` 和 `window_u` 单位均为 W/(m²·K)。条件数组的两列由 `column` 定义：`storeys` 为不超过 3 层/超过 3 层，`shape_factor` 为不超过 0.30/超过 0.30，`inertia` 为 D 不超过 2.5/超过 2.5。窗数组先按 `wwr_bins` 选择行，再选择列。`null` 表示本表未规定，不表示零。

SHGC 是整体太阳得热系数限值，独立于传给 CETUS 的太阳透射率。窗的方向分列保留在 `shgc_directions` 中；外法线 +Y 为北，计算时应用 `EnergyDefaults.north_angle_degrees`。住宅窗墙比按房间同朝向合计，公共建筑按全模型同朝向合计，选择房间子集不会改变该比值。地下部分和绝热部分不计入外立面窗墙比。

默认资源入口已改为 JSON；CSV 仅保留为历史文件及显式 CSV 调用兼容入口。旧的 `climatezone1..5_GB55015-2021_*` 名称映射到各气候区的 A 子区、公共建筑甲类。别名并不判定项目所在地气候子区。新建空间默认使用夏热冬冷 A 区住宅，实际项目应显式选择适用模板。

## 使用接口

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

## 等效模型及假设

- 规范规定的是性能限值，不提供完整材料层。模板按限值建立等效构件；热容量、发射率、吸收率及接地基层热阻等来自 `assumptions`，并非规范规定值。墙/楼板热容量为完整构件值；相邻房间各分配半厚度，在中面绝热，保留室内蓄热。
- 接地表中的 R 是**保温材料层热阻下限**，不直接充当完整构件热阻。计算加入假设的基层 R；周边地面保温下限用于整个地板是已声明的简化场景。没有给定限值的构件使用单独的假设 R。
- 空气到空气 U 值扣除参考表面热阻后传给 CETUS，避免重复计入表面换热。等效 U 包含结构性热桥影响；模型不生成实际热桥节点。
- 夏季等效玻璃采用假设吸收率 0.1，`tau=max(SHGC上限-0.1,0)`，`rho=1-SHGC上限`，使 `tau+alpha` 不超过上限。该模型不代表具体玻璃产品，也不证明冬季 SHGC 下限成立。冬季要求单独记录并给出提示；真实项目可用产品光学参数及逐时遮阳辐照覆盖。
- 运行参数选取附录 C 的参考照明、设备、人员、新风和部分日程。JSON 中的日程是 0～1 系数，Python 侧显式乘以强度转为逐时绝对值；既有命名日程仍按绝对值解释。使用周一开始的典型周，未实现法定节假日、温控升降温过渡及住宅卧室/起居室加权。住宅采用起居室作为代表单区，冷热设定为 18/26℃。
- 人员显热 75 W/人、公共建筑背景渗透 0.5 ACH、热泵制热 COP 2.6 是工程场景假设。制冷 COP 使用参考折算值，不代表选型设备效率；严寒 A/B/C、寒冷 A 住宅统一采用 3.6 是扩展假设，规范表 C.0.7-2 的住宅 3.6 参考值适用范围须另行核对。住宅规范参考换气次数按寒冷地区 0.5、其他地区 1.0 ACH 取值，不重复添加人均新风。

解析结果中每个表面提供 `template_id`、`template_limits`、`template_context` 和逐字段 `sources`。前述限值是选中的规范目标，显式构件覆盖后不表示该构件满足限值。超出窗墙比、体形系数或屋顶透光比例适用范围会给出提示。模板不替代整栋建筑的规范符合性核算。

## 实际导入链路与验收

导入链路为 **JSON → MoosasPy 模板校验与参数解析 → CES3 v2 数值二进制 → CETUS Energy**。Go 引擎接收解析后的构件与运行参数，不直接读取这份模板库；没有恢复旧 i/o 协议。

```powershell
python -B -m pytest test/test_thermal_templates.py -q
python -B test/accept_cetus_energy.py --template climatezone2_GB55015-2021_OFFICE --template-overrides-model --output-dir test/output/cetus_thermal_templates --hours 8760
```

验收脚本保存 JSON 库与引擎哈希、逐表面解析值、边界表、逐时能耗和最终状态。测试覆盖所有 55 个模板的实际二进制注册和计算、自定义 JSON 数值修改、运行覆盖、典型周强度换算，以及三种模型格式的往返。
