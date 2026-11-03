# NBuilding 全年采光实现与验证报告

日期：2026-11-03。代码基准为 develop 提交
`9a4562dfc17162362950b765aed88f464b81ab30`，包含已合并的 1.3.5。
实现位于本地分支 `codex/annual-daylight`。

## 已完成的功能

- 在现有 `simulation/radiation/runner.py` 中增加
  `RadianceRunner.run_annual(AnnualDaylightRequest(...))`，输出
  `AnnualDaylightResult`；没有新建年度执行器类或年度求解文件。
- 共用 PIT 场景、构件光学属性和原生进程执行边界，使用 RADIANCE
  两阶段采光系数与精确太阳直射替换；矩阵组合与指标统计使用现有 NumPy。
- 通过无色材质的通道线性性，在一次追踪中同步累计全部光路与直接光路，
  保证直射替换相减项来自同组采样，避免独立采样差分造成负照度。
  此实现限定于本版明确支持的无色玻璃与漫反射材质。
- 输出总照度、独立直射照度、DA、cDA、UDI、面积加权 sDA 和直射超限面积
  比例 ASE。保留测点所属空间、楼板、实际代表面积、完整时间轴和评价日程。
- EPW 转 WEA 保留原始经纬度和小数时区，校验完整 8760 小时与 DNI/DHI；
  采用当地标准时间的时段中点。PIT 修正经度符号、标准子午线和分钟信息，
  并支持通过 DNI/DHI 调用 `gendaylit` 的天气 PIT。
- 随包包含固定版本 RADIANCE 6.0.2 的 Linux/Windows x86-64 命令行工具；
  Linux 已实测，Windows 完成静态依赖检查，尚未进行 Windows 实机运行验证。

## TTL 及性能模块之间的实际传递

继续复用构件、空间的 `settings`，未新增 RDF 类。标量保持标准 RDF 类型，
复合值沿用既有 JSON literal，支持嵌套字典、数组、布尔值和数值字符串。
独立遮阳属性也可往返。修正了规范属性三元组可能被旧元数据快照覆盖的问题，
现在手工编辑 TTL 中的规范属性后，重新加载会使用该编辑值。

本次主测试使用的是**用户上传的 NBuilding.rdf**，不是仓库同名测试样例。
原始 SHA-256 为：

`8321e695825c2a6a6276d0b6d245723defda858b8fd8118f08ff00aa274e74be`

模型包含 37 个空间和 275 个构件：117 面墙、96 块窗、59 个水平构件、3 个天窗。
对全部构件写入嵌套属性，对全部空间写入 8760 项评价掩码，再保存和加载 TTL，
逐项验证值未丢失。另对单窗、单墙设置有意不同的属性，检查实际模块输入：

| 对象/属性 | 测试值 | 检查结果 |
|---|---:|---|
| `gls_0_206` 可见光透射率 | 0.61 | 已进入 RADIANCE 材质转换与 manifest |
| 同窗太阳透射率 | 0.42 | 已进入 Energy `SurfaceRequest.transmittance` |
| 同窗开窗比例 | 0.37 | 已进入 CONTAM 路径 `operable` |
| 同窗流量系数 `airflow_element.cd` | 0.61 | 已进入 CONTAM 原生 element 字典 |
| `wall_0_132` 可见光反射率 | 0.52 | 已进入 RADIANCE 材质 |
| 同墙太阳反射率 | 0.31 | 已进入 Energy `SurfaceRequest.reflectance` |
| 空间评价掩码 | 每日 08:00–18:00 | TTL 往返保留，年度指标每空间分母为 3650 小时 |

Energy 输入包含 37 个热区；通风输入包含 37 个区、136 条路径。
此处验证的是新属性的保存、加载与模块输入传递，不表示本轮重新进行了完整能耗
或通风年度精度标定。既有相关模块测试已纳入全仓回归。

## 完整年度试算

使用包内北京南苑 TMY 气象作为可重复的软件测试边界。固定原模型几何与遮阳状态，
未给出可见光属性的构件使用 manifest 中明示的默认值。上表单窗、单墙为属性传递
探针，故下列指标属于该测试场景，不能作为建筑真实玻璃和占用条件的测量结论。

| 项目 | 结果/参数 |
|---|---:|
| 分析空间 | 37 |
| 分析面积 | 1792.82 m² |
| 网格间距 / 工作面高度 | 0.5 m / 0.76 m |
| 测点 | 7853 |
| 时间列 | 8760 |
| 总照度 / 直射照度矩阵 | 各 7853×8760，float32，lux |
| 天空基底 | Reinhart MF=2，578 行含地面 |
| 反射深度 / 采样数 | 5 / 4096 |
| 原生并行进程 | 6 |
| 年度求解和输出耗时 | 211.53 s；模型与输入准备另约 5.97 s |
| 全部空间面积加权 sDA300/50% | 85.90% |
| 指定日程下 ASE1000,250 | 41.33% |
| 最大总照度 / 最大直射照度 | 50315.43 / 41278.64 lx |
| 全矩阵物理约束 | 总照度 ≥ 直射照度 ≥ 0，全部通过 |
| 最小“总照度−直射照度” | 0 lx |

两份照度数组共 550,338,240 字节，不通过 JSON 或 TTL 传递。
最终输出没有负照度截断警告；保留固定遮阳口径和可见光默认值两项提示。

## 数值交叉检查

1. **解析反射场景**：单位亮度均匀天空/地面，单面 50% 反射墙遮挡约半个
   测点半球。全部光路照度系数接近 `0.75π`，直接光路接近 `0.5π`，
   均通过 2% 误差检查；全部系数不小于直接系数。
2. **独立直射检查**：无遮挡场景年度最强直射时刻与单太阳 `rtrace` 对照，
   通过 1% 相对差检查。夜间列保留且为零。
3. **同天气 PIT 对照**：在完整 NBuilding 的 7853 点中等距抽取 32 点，采用
   相同坐标和天气，以 `gendaylit+rtrace`、8192 采样检查四个季节时刻。
   下表误差定义为 `mean(abs(Eannual−EPIT))/mean(EPIT)`，不是逐点相对误差的均值。

| 当地标准时间 | 样本平均 PIT / 年度照度（lx） | 归一化平均绝对差 |
|---|---:|---:|
| 01-21 12:30 | 760.57 / 812.17 | 6.78% |
| 03-21 12:30 | 949.73 / 992.41 | 4.61% |
| 06-21 12:30 | 995.11 / 1012.17 | 2.06% |
| 09-21 15:30 | 1247.34 / 1282.94 | 5.22% |

4. **局部年度收敛**：24 个测点的单空间场景，比较 MF=2/4096 采样与
   MF=4/16384 采样。逐点 DA 最大差 1.53 个百分点，UDI 有效区间最大差
   1.70 个百分点，空间 sDA 差 0。该检查使用 2 m 网格，不替代全楼网格或
   全年独立参考模型的全面验证。

## 测试与可重复执行

最终全仓测试：**545 passed，8 skipped，0 failed**，耗时 371.06 s。
12 项既有警告来自 IDF 模板缺少地温表时的显式回退提示。
其中新增采光测试 9 项，包括面积/孔洞、多片区域、指标边界与夜间分母、
小数时区、缺测拒绝、TTL 编辑优先级与类型、原生年度直射、天气 PIT 和解析反射。

```powershell
$env:PYTHONPATH = (Get-Location).Path
$env:OPENBLAS_NUM_THREADS = '2'
& 'D:\miniconda3\Scripts\conda.exe' run -n moosas python -m pytest test -q
& 'D:\miniconda3\Scripts\conda.exe' run -n moosas python test\run_annual_nbuilding.py `
  path\to\NBuilding.rdf results --processes 6 `
  --probe-window gls_0_206 --probe-wall wall_0_132
& 'D:\miniconda3\Scripts\conda.exe' run -n moosas python test\check_annual_pit.py path\to\results\moosas-annual-<id>
```

[机器可读验证记录](validation/annual_daylight_20261103/NBuilding_validation.json)、
[空间指标](validation/annual_daylight_20261103/metrics_spaces.csv)、
[PIT 对照](validation/annual_daylight_20261103/pit-comparison.json)、
[局部收敛记录](validation/annual_daylight_20261103/convergence.json)、
[全仓测试日志](validation/annual_daylight_20261103/pytest.txt)。
接口、字段与物理假设见 [annual_daylight.md](annual_daylight.md)。

## 当前能力边界

本版为无色普通玻璃、漫反射表面、固定遮阳下的年度分析。未实现可操作百叶、
复杂 BSDF 或完整 LM-83 遮阳运行评估；sDA/ASE 必须附带本次实际日程和遮阳状态。
不支持闰年、不完整年份与斜工作面，相关输入明确报错。天气 PIT 与年度默认
网格不同，比较时须使用相同测点。首版不自动复用跨次运行的传输系数缓存。
Windows 实机和全部参考场景的全年独立高精度对照仍是后续验证范围。
