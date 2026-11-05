# energyv135 接入 CETUS 1.9.4

基线：`KLEGB/MOOSAS`，`energyv135`，`eab365642545ad0d3b9da9f7f69d2d3beac2ef07`（2026-11-04）。
CETUS 对照来源：本对话交付的 `CETUS_1.9.4_clean.zip`。不涉及热湿舒适等级评价。

## 已有流程与最小修改

MOOSAS 的 `simulation/energy/input.py` 将每个 Space 转换为独立 ZoneRequest，并构造显式边界、构造层、小时内扰、温控和室外空气流量。
`runner.py` 使用 CES3 v2 持久进程求解显热；EnergyResult 的四项能耗分别是制冷、供热、照明、设备电耗。
1.9.4 的潜热属于原生显热解之后的水汽平衡，因此直接扩展这两个文件和既有 Request/Result，未新增业务模块或数据类。
原生显热协议和所带 Windows/Linux 可执行文件继续使用；不新增引擎依赖、编译、SHA 检查或系统设备模型。

参数使用现有模型和空间 settings。RDF 原有 energy metadata 已支持任意 JSON 参数及日程，不增加 RDF 类、谓词或墙窗实体：

|现有保存位置|键|单位/缺省值|
|---|---|---|
|model.energy_settings|latent.enabled|bool，默认 false|
|Space.settings 或 zone_overrides|zone_popmoisture|kg/(h·人)，默认 0.06，可用日程名|
|Space.settings 或 zone_overrides|zone_c_hum|0–1，优先已有值，缺省 0.6|

人员密度、新风、渗风、夜间通风和机械补风继续使用原有参数与作息。
显式机械换气视为原接口已经定义的室外补风等效量，不把系统送排风设计容量自动转成外气。
湿度控制遵循 `hvac_enabled`。有控制时为理想 RH 上限，无控制时计算自由稳态；零交换有湿源时无控稳态不存在，明确输出缺失值和 warning。
不模拟蒸汽加湿、再热、盘管容量、湿容量、材料湿迁移、空间间水汽交换，不追加潜热电耗或耦合热源。

接口示例和完整结果字段见 `MoosasPy/libs/energy/README.md`。

## 接入必须处理的基线问题

1. DeST CSV 含湿量是 g/kg，但原 WeatherData 直接保留该值。现于读取处除以 1000，统一为 kg/kg。原 EPW 多项式还会在北京冬季产生负产物，已改用露点及实际气压计算含湿量，再按原 CSV 的 g/kg 格式输出。空气温度、气压、辐射等显热边界不受此次单位修正影响。
2. 基线 RDF 加载无条件导入缺失的 `adapter_openings`，导致普通 NBuilding 也无法加载。现有普通 RDF 不再依赖该可选读取器；若图中含 adapter 专用谓词，缺失读取器仍明确报错，未声称实现遗失的 adapter 数据能力。
3. 基线能耗和辐射引用缺失的 `geometry.planar`。在已有 `geometry/geos.py` 中补齐投影面修复和多边形提取函数，调整原调用导入，保留孔洞及多面；没有新建几何模块。

## NBuilding 验证

实际输入：仓库 `test/caseFile/NBuilding.rdf`，37 个热区，总面积 1,793.52 m²。气象为随库北京南苑 TMY，属于软件接入测试，不代表 NBuilding 场地标定。
采用既有未遮挡天气太阳投影，不运行新的 Radiance 年度采光，也不修改已有采光功能。

- **全年**：37 × 8,760 = **324,120 个热区小时**，分别运行潜热关闭和开启两遍。
- 两遍周期预热均为 15 轮，采用既有 0.1 K 容差。
- 开关前后室温最大差 **0 K**；既有逐时能耗最大差 **0 kWh**；最终热状态完全一致。
- 原有全年四项电耗合计均为 **161,164.023421 kWh**，没有加入潜热电耗。
- 开启后显热冷负荷 **129,379.567351 kWh**；潜热冷负荷 **17,492.220751 kWh**；总冷负荷 **146,871.788102 kWh**。
- 理想除湿质量 **25,810.055517 kg**；最大湿量残差 **1.3553×10⁻¹⁹ kg/s**。
- 最大热平衡残差 **2.2259×10⁻⁸ W**。全年湿度结果无非有限值。
- 此次单次全年运行约 37–39 s，不作加速或额外开销的统计结论。

新增集成测试还实际验证 2 月 1 日和 7 月 14 日的全部热区，包含预热、逐时回调、原生结果逐字段相等、RDF 保存/重载、产湿日程及湿度参数恢复、无控湿平衡、干燥外气抵消和非法参数。
原模拟契约/天气/模型 I/O 回归：**98 passed, 1 skipped**（因未提供 office 模板附件而跳过一项）。
新增潜热与天气基础测试初次为 **15 passed**；最终聚焦测试为 **21 passed**，覆盖新增测试、天气及原生资源契约。机器记录随交付保存。

另行尝试收集基线 `test/test_annual_daylight.py` 时，其导入不存在的 `prepare_daylight_weather` 失败，这是基线已有缺项，本轮未扩展全年采光以处理它。因此不声称整个仓库所有测试通过。

这次验证确认潜热正确进入 MOOSAS 的输入、求解后处理、参数持久化和结果接口，不能视为新增 ASHRAE 潜热验证或真实建筑湿度精度验证。基线在上述路径修复前不能直接完成 NBuilding，因此数值相等结论具体指同一修复后显热路径下的潜热开关对照。

复现：

```bash
python -m pytest test/test_cetus194.py test/test_weather_architecture.py test/test_native_resources.py -q
python -m test.run_cetus194_nbuilding --output test/output/cetus194
```

机器报告：`test/output/cetus194/NBuilding_validation.json`。
完整热区逐时输出：`NBuilding_hourly.csv` / `NBuilding_results.npz`。
