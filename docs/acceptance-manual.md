# 真实环境验收操作手册

本手册用于**真实环境**下完成剩余 19 项 blocked 任务的验收。原则：**凡不能证明的一律保持未勾/blocked，绝不补零、绝不挑样本、绝不从合成 fixture 推默认值**。离线部分已全部完成（进度见 `openspec`）；本手册只覆盖需要真实 PostgreSQL / 三产品 / 特权机的部分。

> 通用：使用 `.venv/bin/python`；凭据只经环境变量，**不写入命令参数、产物或提交记录**；每步保存原始证据并在报告标注 `environment_verified`。

---

## 0. 通用准备

```bash
cd /storage/Users/currentUser/IDEProjects/sandbox_graph
.venv/bin/python -V                 # 期望 3.14.x
.venv/bin/python -m pytest -q       # 基线：应 849 passed, 4 skipped
mkdir -p evidence/real/<group>/<timestamp>
```

**证据格式（每项）**：命令原文、退出码、原始输出、环境版本（PG/产品/内核）、执行时间、`blocked_reason`（若有）。产物写入 `evidence/real/...`，不进入模型上下文。

---

## A. 真实 PostgreSQL（组 3 / 4 / 11.2 / 14.1）

### A.0 先决条件
- PostgreSQL 16+，账户有**创建/删除临时 schema** 权限。
- 变量：`SANDBOX_GRAPH_POSTGRES_DSN`（运行）、`SANDBOX_GRAPH_TEST_POSTGRES_DSN`（测试）。

### A.1 建库与迁移（组 1.1）
```bash
export SANDBOX_GRAPH_POSTGRES_DSN='postgresql://USER:PASS@HOST:5432/DB'
.venv/bin/python -m sandbox_graph migrate
.venv/bin/python -m sandbox_graph status RUN_ID
```
- **验收**：迁移建出 `orchestration_records`、`thread_leases`、官方 saver 表；最小连通与 `status` 查询成功。
- **不通过**：无服务端连接证据 → 标 blocked，不得写"已验证支持范围"。

### A.2 L2 持久化/租约/fenced saver（组 3.1/3.2/4.1/4.2/4.3）
```bash
export SANDBOX_GRAPH_TEST_POSTGRES_DSN="$SANDBOX_GRAPH_POSTGRES_DSN"
.venv/bin/python -m pytest tests/orchestration/persistence -q -rs
```
- 覆盖：`test_postgres.py`（真实 PG、双 worker、barrier、FencedPostgresSaver 与 lease 行 CAS、接管 fencing）。
- **验收**：**无 skip**；并发只有唯一 owner、旧 epoch 写入被拒、未知域隔离、checkpoint/pending writes 与 claim 同事务。
- **说明**：任务点名 `test_repository.py`／`test_receipts.py`／`test_leases.py`／`test_fenced_saver.py`／`test_owner_takeover.py`；当前拆分为 `persistence/test_postgres.py` + `recovery/controlled_runner.py`。若需逐文件名对齐，先补这些文件（离线可写，运行仍跳過）。

### A.3 图版本激活竞态（组 11.2）
- 目标：`test_revision_activation.py`（**当前未实现**）。实现后：
```bash
.venv/bin/python -m pytest tests/orchestration/unit/test_revision_activation.py -q -rs
```
- **验收**：两并发候选只有一个成功、`request_id` 幂等、predecessor CAS、崩溃恢复不产生半激活。

### A.4 组合故障硬杀（组 14.1）
- 目标：`test_recovery_integration.py`（**当前未实现**）。用持久 `ControlledRunner` + 真实 PG 覆盖：双 worker 硬杀、连接中断、重复/乱序事件、artifact 提交失败、图版本切换。
- **验收**：无双重提交、无越权清理、残留可审查；失败保留原始账本。

---

## B. 三产品真实接入（组 17 / 19 / 26.2 / 26.3 / 14.2）

### B.0 先决条件
- 每产品一套可用部署 + 访问凭据（控制面 API Key、数据面 token）、版本号。
- bench 配置（`config.yaml`）：`product`、`api_url`、`data_url`、`source`、`create_body`、`source_evidence`、`environment_evidence`（见 `docs/orchestration/setup.md` 示例）。

### B.1 最小功能与预检（组 26.2）
```bash
# 1) 端点拓扑解析（只读）
.venv/bin/python -m sandbox_bench.runner endpoint --case case.yaml --output out/endpoint.json --config config.yaml
# 2) 来源身份只读解析
.venv/bin/python -m sandbox_bench.runner resolve --case case.yaml --output out/resolve.json --config config.yaml
```
- **验收**：输出 `resolved`（或明确 `blocked`/`unverified`）；产品版本写入证据；缺连接/权限/版本 → blocked。
- **预热池**：确认 Kuasar 预启动 runner 单元、Cube TAP/cgroup 池、AgentENV 设备池**不出现为可删除沙箱**。

### B.2 基础性能/密度/固定 Q（组 26.3）
```bash
.venv/bin/python -m sandbox_bench.runner case --case case.yaml --config config.yaml --output out/case.json
.venv/bin/python -m sandbox_bench.runner verify out/case/evidence
.venv/bin/python -m sandbox_bench.runner recompute out/case/evidence
```
- **验收**：`BTC-060` 可追溯结果、缺口、干扰、真实分母齐全；未确认 Q12 时净底噪不输出假数；扩展未选不执行。
- **独立抽查**：抽查 ID 与原始事件，离线 `recompute` 必须与线上一致。

### B.3 五阶段 E2E（组 14.2）
- 目标：`test_end_to_end.py`（**当前未实现**）。双环境组、部分失败、人工等待、增量分析、only-analysis/report。
- **验收**：`ORC-065–068`；所有已选项有显式处置、Excel/报告同版本可追溯；真实产品测试状态单列。

---

## C. 特权机：eBPF 真 CPU 峰值（组 31.4）

### C.0 先决条件
- root 或 `CAP_BPF`；`clang`；`bcc`；`tracefs`（`/sys/kernel/tracing`）。
- 自检：
```bash
.venv/bin/python -c "from sandbox_bench.sched_peak import BpfSchedSwitchCollector as C; print(C.availability())"
# 期望 {'available': True, 'backend': 'bcc', ...}
```

### C.1 采集并对比
- 采集器：`BpfSchedSwitchCollector(run_id, clock_domain='bpf:ktime:<boot_id>')`，`start/poll/stop`，`peak(attribution=..., window_start_ns=..., window_end_ns=...)`。
- 用 `attribution_from_inventory` 按**进程类**归属（管理 agent / 沙箱 VMM），与 `sampled_peak_cores` **分列**；窗口需 pre-roll/post-roll。
- **验收**：`true_peak_cores` 与 `sampled_peak_cores` 均记录且标注口径；trace 时钟与 Python monotonic **不混算**；无特权/无 bcc → `unavailable` 回退 sampled。

---

## D. 真实进程/拓扑发现（组 32.5 / 33.5）

### D.0 只读探针
- 输入：`systemd` unit 列表、`/proc/<pid>/cmdline`、cgroup、`boot_id`、`start_ticks`、sandbox generation、容器运行时。
- 步骤：
```bash
# 1) 声明拓扑目标
.venv/bin/python -m sandbox_bench.runner topology --config topology.yaml --output out/topology.json
# 2) 发现进程 → 归属（build_inventory 的输入为探针输出）
```
- **验收**：`build_inventory` 把真实进程映射到声明组件（Kuasar `node-ctl conductor/proxy/...`、`cloud-hypervisor`；Cube `cubelet/cubevs/cube-runtime/cube-hypervisor`；AgentENV `server/ublk-daemon/firecracker`）；**未知/歧义进程标 unresolved，不归总**；单机共置与多机两种部署各验证；不可解析组件 `blocked`。

---

## E. 环境还原真实核验（组 34.4 / 35.4）

### E.0 配置（还原合同）
`config.yaml` 增加：
```yaml
restore: { batch_size: 5, batch_gap_s: 2, max_rounds: 200, baseline_ids: [] }
```

### E.1 测试间还原
```bash
# 记录基线：测试前快照本轮 run_id 归属实例
.venv/bin/python - <<'PY'
import yaml,json
from sandbox_bench.adapters import Adapter
cfg=yaml.safe_load(open('config.yaml'))
a=Adapter(cfg); rows=a.list_owned(cfg['run_id'],10)
json.dump([r['sandbox_id'] for r in rows],open('out/baseline.json','w'))
PY
# 还原（小批量多轮 + 残留核验）
.venv/bin/python -m sandbox_bench.runner restore --config config.yaml --output out/restore.json
```
- **验收**：`status=='complete'` 且 `residual_ids==[]`；**只删本轮 run_id 归属**；预热/空闲/基线实例**不被删**；删除为异步的产品（Kuasar）在核验前等待 settle；`rounds` 记录每轮删除数（非一次性全删）；单机与多机各验一次。
- **失败**：非空残留或删除失败 → `incomplete`，保留残留身份与恢复入口。

---

## F. B00 构建指标与背景密度真实核验（组 36.4）

### F.1 构建指标
```bash
.venv/bin/python -m sandbox_bench.runner build --case b00_case.yaml --output out/build.json --config config.yaml
```
- **验收**：`resource_peak{pss_bytes,cpu_seconds}`、`artifact.size_bytes`、`cache_state`（冷/热）**由真实采集填充**，非 `None`/`unknown`；缺证据标 `unavailable` 不补零。
- 冷/热缓存：分别以冷缓存与热缓存各跑一次并**分列**。

### F.2 背景沙箱密度
```yaml
# b00_case.yaml
background: { background_count: 50 }   # 先建 50 个背景沙箱
```
- **验收**：`build.background.requested==actual`（背景实际创建数）；构建在背景存在下执行；`background.incremental_resource` = 构建期峰值 − 背景基线；背景沙箱**不计入**产物分母；**不要求并发构建密度**；背景不足 → `blocked`。

---

## G. 背景条件与合同样例真实核验（组 37.4 / 38.3）

### G.1 逐场景条件声明与对账
- 对每场景按 BCD-16 的 R 集声明背景条件，并用**实际实现值**对账（SP-03、实际 N/C/λ）：
```bash
.venv/bin/python -m sandbox_bench.runner scenario --case case.yaml --output out/scenario.json
```
- **验收**：R 集字段齐、O 集缺省标 `not_selected`；实际值与声明不一致 → 标**条件受限**，不当达成。

### G.2 逐场景样例采集
- 对三产品 × 方法矩阵，按 `scenario-spec.md` 的 R 集采集并回收证据。
- **验收**：单机共置与多机分别声明并采集；证据可 `recompute` 复算；不可比项保留条件。

---

## H. 逐任务验收清单（19 项）

| 任务 | 类别 | 关键命令 | 通过判据 |
|---|---|---|---|
| 1.1 | PG | `sandbox_graph migrate` / `status` | 迁移+连通成功，版本记录 |
| 3.1/3.2 | PG | `pytest tests/orchestration/persistence` | 无 skip，UoW/receipt 通过 |
| 4.1/4.2/4.3 | PG | 同上 | 双连接/ fenced saver/接管通过 |
| 11.2 | PG | `test_revision_activation.py`（待写） | 并发激活唯一成功 |
| 14.1 | PG | `test_recovery_integration.py`（待写） | 组合故障无双重提交 |
| 14.2 | 产品 | `test_end_to_end.py`（待写） | 五阶段 E2E ORC-065–068 |
| 26.2 | 产品 | `runner endpoint`/`resolve` | 端点/来源 resolved 或 blocked |
| 26.3 | 产品 | `runner case`/`verify`/`recompute` | 可追溯结果+真实分母 |
| 31.4 | 特权 | `BpfSchedSwitchCollector` | 真峰值与采样峰值分列 |
| 32.5 | 真实发现 | 只读探针 + `build_inventory` | 进程→组件一一对应，未知不归总 |
| 33.5 | 真实+多机 | `runner topology` | 声明组件映射真实进程；单/多机 |
| 34.4 | 还原 | `runner restore` | 残留空；预热/基线不删 |
| 35.4 | 还原(图侧) | N09 收尾 | 孤儿被扫描捕获；残留→partial |
| 36.4 | B00 | `runner build` + 背景密度 | 指标填充；背景增量资源 |
| 37.4 | 条件 | `runner scenario` + 实际对账 | 声明/实现一致或标受限 |
| 38.3 | 样例 | 按 `scenario-spec.md` | 三产品×方法样例可复算 |

**待补测试文件（离线可写、运行需真环境）**：`test_repository.py`、`test_receipts.py`、`test_leases.py`、`test_fenced_saver.py`、`test_owner_takeover.py`、`test_revision_activation.py`、`test_recovery_integration.py`、`test_end_to_end.py`。

---

## I. 阻塞与失败处理

- 任一先决条件缺失（无 PG/无产品/无特权）→ **明确 `blocked` 并保留未勾选**，不得以受控 HTTP 或合成 fixture 替代。
- 真实值不得回填历史样本；`repeat`/`generation` 追加而非覆盖。
- 失败保留原始账本与资源状态；未知副作用 `quarantine`；不补零、不裁剪。

## J. 回退与清理

```bash
.venv/bin/python -m sandbox_bench.runner restore --config config.yaml --output out/restore.json   # 只删本轮归属
.venv/bin/python -m sandbox_bench.runner cleanup --config config.yaml --output out/cleanup.json    # 所有权清理
```
- 预热池/他轮/基线实例**永不删除**；残留单列。
- 归档：`evidence/real/<group>/<timestamp>/`，含命令、退出码、原始输出、版本与 blocked 原因。
