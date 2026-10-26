# 测试规格矩阵（场景 × 规格 × 并发 × 请求 × 条件）

本文件把**实验条件轴**显式化，配合 [scenario-spec.md](../openspec/changes/sandbox-evaluation-workflow/scenario-spec.md)（逐场景指标/条件）与 [runtime-evaluation RT-19](../openspec/changes/sandbox-evaluation-workflow/specs/runtime-evaluation/spec.md)（规格作为独立条件轴）使用。

## 轴（每个用例至少固定这些，不得混组）

| 轴 | 取值 | 说明 |
|---|---|---|
| 场景 / 方法 | `B00–B10`、`E01–E06` | 逐场景以 `scenario-spec.md` 为准 |
| 来源 kind × 启动路径 | `image_artifact/sandbox_artifact/snapshot_manifest` × `cold_boot/disk_restore/memory_restore` | 不同来源/路径**不得合并**时延 |
| **沙箱规格** | `2v2G`、`2v4G`、`4v8G`、`8v16G`（具体档位与 G 单位按部署确认） | RT-19：规格是独立条件，跨方案按规格分组比较 |
| **并发 C** | `1`、`2`、`4`、`8`、`16` | 创建/密度类必测；C 变化必分格 |
| **请求总量 Q / 负载窗口** | `3`、`10`、`50`（WINDOW）；或 BATCH 的 `(B, C)`；或 OPEN_LOOP 的到达率 | 失败消耗预算、不补发到成功数 |
| 重复轮次 | ≥3（报告分位、不跨轮平均） | 每轮独立 fixture |
| 完成终点 | `API_COMPLETE`/`STATE_READY`/`SERVER_TERMINAL`/`EXEC_COMPLETE`（+白盒 `GUEST_READY`） | 必须与审计的入口同步性一致（见 benchmark-wiring.md） |
| **口径版本（client caliber）** | `create.header_stop.reuse.warmup.v1`（复用+预热）/ `create.header_stop.oneoff.v1`（单次） | RT-47：停止于**响应头**（`body_read_s` 单列）；不同口径**不可比** |
| 背景条件 | RT-45 目录（N/C/W/SP/D/L/TL/EP/DEP） | 逐场景 R/O/— 见 BCD-16 |
| 指标 | RT-44 目录（T/H/R/S/C/A/B） | 逐场景 R/O/— 见 BCD-15 |

## 逐场景适用（摘要，细节见 scenario-spec.md）

| 场景 | 规格 | 并发 C | 请求 Q / 窗口 | 备注 |
|---|---|---|---|---|
| B01 创建、B02 镜像创建 | **必测多档**（2v2G/2v4G/4v8G…） | **必测多档**（1/2/4/8…） | **必测多档**（Q 或到达率） | 端点按 RT-42；同步入口直测、异步须密集轮询 |
| B09 底噪、B10 密度 | 必测（≥2 档） | 密度/滑窗/分批对比 | 固定 N 或固定 Q_total | 同步采 PSS/CPU，按实际存活数算均值 |
| B03 暂停、B04 恢复、B05 克隆、B08 快照 | 固定 1 档（源规格） | 通常 1 | 单次或按 fork 数 | 与创建分开计时 |
| B06 删除、B07 首次/输出/错误 | 固定 | 视方法 | 单次 | 终点与清理分别记录 |

## 默认矩阵（建议起点，按环境与预算裁剪）

- **创建类（B01/B02）**：`规格 {2v2G, 2v4G, 4v8G} × C {1,2,4,8} × Q {3,10,50} × 重复 3`（= 108 组，可裁剪）；
- **密度类（B09/B10）**：`规格 {2v2G, 2v4G} × C {1,2,4,8} × 目标 {50,100,200}`（固定 N 与固定 Q_total 各一）；
- 所有格子保留：来源身份、端点、条件、每轮 fixture、原始样本与资源序列。

## 边界与诚实性

- **不同规格 / 并发 / 请求 / 端点 / 口径版本的样本不得混成一个时延或密度结论**（RT-19、RT-42、RT-47）。
- 失败消耗请求预算，不补发到成功数；`insufficient`/`unavailable` 保留原样，不填零。
- **现状**：`scripts/matrix_run.py` 用离线 mock 跑矩阵（合成样本，真图/真调度/真证据），仅验证**编排与分格**；真实数值需真实环境。mock 的合成模型当前只在 **C/Q** 上有区分，**规格轴无差异**（真实产品才会有）。

完整矩阵输入现由 `config/case-matrix.yaml` 与 `sandbox_bench/casegen.py` 实现。生成、图执行和证据字段见 [完整用例使用说明](conditions-profile.md)。选择用例仍通过 Selection，条件组合通过 axes。
