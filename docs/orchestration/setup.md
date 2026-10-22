# 编排启动与验证

> 真实 PostgreSQL／三产品／特权机的逐项验收步骤见 [acceptance-manual.md](../acceptance-manual.md)。本文件只说明启动、依赖与最小验证。

本目录交付的是部分可执行控制面，不是主变更完成声明。运行结果、未完成工作和数据库验收边界见 [implementation-report.md](implementation-report.md)。公共接口见 [contracts.md](contracts.md)。

本机已导入并执行的版本：Python 3.14.7（HarmonyOS），LangGraph 1.2.16，langgraph-checkpoint 4.3.0，langgraph-checkpoint-postgres 3.2.0，psycopg 3.3.6，Pydantic 2.14.1，pytest 9.1.1。版本安装与根锁文件由项目统一维护。尚未建立 Linux/Python 多版本 CI 矩阵。正式数据库目标 PostgreSQL 16+；没有服务端连接证据，不能将该目标写成已验证支持范围。

```bash
.venv/bin/python -m pytest tests/orchestration -q
.venv/bin/python -m sandbox_graph catalog
.venv/bin/python -m sandbox_graph preview selection.json
```

`preview` 的 JSON 包含 `catalog`（Case 数组）、`selection`、`authorized_groups`，可选 `evidence`、`graph_ref`、`contract_ref`。CLI 预览是离线结构预览，不写库；正式启动使用 `RunService.preview/start`，在同库保存预览并引用完全相同 plan_hash。

```json
{
  "catalog": [{"case_id":"synthetic-case","group_id":"G_A","sandbox_type":"synthetic","scenario_id":"B04","method_id":"B04.MEMORY","priority":"base","capability":"supported","adapter_verified":true,"conditions":[{"fixture_size":1}],"dependencies":["fixture.pause"],"conflict_domains":["synthetic-host"]}],
  "selection": {"groups":["G_A"],"scenarios":["B04"]},
  "authorized_groups":["G_A"],
  "graph_ref":"run:1",
  "contract_ref":"synthetic:1"
}
```

示例全部是显式声明能力的合成合同，不是实际环境、规格、权限或测量预算。真实Case省略capability/adapter_verified时默认unverified/false，必须经部署核验才能显式声明支持。

正式数据库通过环境变量 `SANDBOX_GRAPH_POSTGRES_DSN` 配置；不要把密码写入命令参数、产物或提交记录。初始化：

```bash
.venv/bin/python -m sandbox_graph migrate
.venv/bin/python -m sandbox_graph status RUN_ID
```

正式后端只有 `PostgresRepository`。未配置连接会直接失败，无 SQLite/内存回退。迁移建立 `orchestration_records(kind,key,revision,body)`、`thread_leases` 及官方 saver 表。逻辑表以 kind 隔离、业务键唯一；这与设计中每种记录一个物理表不同，属于当前简化，不宣称已经做过数据库性能验收。控制面事务使用全局 PostgreSQL advisory lock；外部执行不持该锁。所有 saver 写仍额外锁实际 `thread_leases` 行，和接管使用同一行。

真实集成测试使用独立测试 DSN，账户需要创建/删除临时 schema 的权限：

```bash
# 在环境中配置 SANDBOX_GRAPH_TEST_POSTGRES_DSN 后运行
.venv/bin/python -m pytest tests/orchestration/persistence -q -rs
```

每例创建随机 `orc_*` schema，结束删除该测试 schema。没有 DSN 会显式 skipped，不能将 skipped 写成通过。当前本机没有 PostgreSQL 服务，也没有执行正式事务竞态和 controller 硬杀恢复。

应用集成需要显式注入 `RunnerGateway`、`EnvironmentProbe`、统计/比较/Excel/报告服务及持久 ArtifactStore。`Driver` 接收按本次不可变 LeaseToken 构造的 saver_factory。正式工厂必须返回 `FencedPostgresSaver`，禁止共享一个可变全局 token。纯测试才可显式注入 InMemorySaver。`runtime.bootstrap.build_job_handlers` 组装固定 graph_ref 的 run/group/work/analysis/report/resume，以及配置 OperationService 后的 cleanup/reconcile；`Worker.run_once/serve` 消费任务、outbox、watchdog 和精确等待。业务工厂注入实际 EnvironmentHandlers、benchmark handlers 和 AnalysisReportServices。未知 job_kind 持久标为 blocked。retry 新 attempt 执行器仍未提供；CLI 不隐式启动远端实验。五图组件与组增量交付已本地验证，完整真实 PostgreSQL worker 进程验收尚未执行。

## 恢复顺序

1. 保留原 graph/version、代码和原 run/thread；数据库恢复前停止新 claim/提交。
2. 等旧 driver lease 到期，获取新 epoch；资源 reservation 不随 lease 到期删除。
3. 读取旧 activation 和 receipt；checkpoint 落后时同 visit 直接复用 receipt。
4. `submission_started/running/uncertain` 调用原 operation 的 reconcile，不重新造 operation_id。
5. 无法证明外部已停止时保留 quarantine；已知资源按 scope 单独取消、清理并记录剩余。
6. 新图只用新 run/thread，旧图保留 drain/cleanup 入口。已发布文件无账本引用属于 orphan，尚无自动 GC 服务。

### quarantine / 残留 lease 目前没有自动回收

`SchedulingService.dispatch_work` 对**任何存在的** `resource_leases` 行（`quarantine` 或崩溃残留 `reserved`）一律置 `queue_reason='resource_conflict'` 并永久跳过该 work；除 `LeaseService.release(..., closed=True)`（要求清理已核验）与 `release`/已验证清理路径会 `delete` 外，**没有回收/reconcile 入口**，因此一次未核验清理（例如缺资源证据导致 `resources` 未关闭）会持续阻塞后续所有 run。

当前唯一处置是管理员核对证据后手工删除对应行：`DELETE FROM orchestration_records WHERE kind = 'resource_leases' AND key = '<conflict_domain>'`（先确认对应 scope 的外部效果已确实停止）。自动回收/reconcile 命令尚未实现；不要把它当作已具备的能力。

缺原版本定义、原始文件、真实适配或授权合同应明确阻塞；不能换用当前图解释旧 checkpoint，也不能补空证据或测量值。

## 显式应用装配

```python
from sandbox_graph.runtime.bootstrap import build_job_handlers
from sandbox_graph.runtime.worker import Worker, WorkerPolicy

handlers = build_job_handlers(
    repository, graph_ref="run:1", saver_factory=fenced_saver_factory,
    node_service_factory=registered_services_for_activation,
    review_service=reviews, deadline=run_deadline,
    lease_seconds=30, segment_seconds=120,
    delivery_finalizer=delivery_finalizer, operation_service=operations,
)
worker = Worker(repository, handlers,
    policy=WorkerPolicy(claim_seconds=30, job_seconds=150, poll_seconds=1))
await worker.serve(stop_event)
```

代码中的 repository、真实版本化服务、fenced saver connection 生命周期和截止由部署应用提供。`registered_services_for_activation(token, activation)` 在取得真实 token 后构造 closures；不得从 scope 字符串伪造 owner token。保持该 worker 的 graph_ref 与已部署原版本一致；尚无跨版本自动代码加载器。服务实现参考 [分析与报告接入](../analysis-report.md)。

只分析/只报告通过 `RunService.start` 显式 evidence.source_run_id 绑定原运行，并分别提供 analysis_input_ref/analysis_ref；授权必须覆盖冻结组，计划和图版本必须匹配。该入口只派发纯分析或报告，不创建测量任务。`reanalyze/rerender` 同样精确绑定输入，使用独立 command thread。

复用 producer 绑定、accepted 自动投影及 R6/A5 显式有界分析返工接入见 [reuse-rework.md](reuse-rework.md)。旧交付没有 measurement_bindings 或未启用显式 reuse policy 时不可默认复用。当前外部 supplement/remeasure 动作缺注册执行器即终态 blocked，不产生待消费空任务。

## 交付件（数据／日志／报告）落在哪里

单次测试的中间与最终交付件分**两处**，且都**不会自动出现在源码树里**。

### 1. 会话／输出目录（普通目录）

- 单用例 CLI：`sandbox_bench.runner case --case case.yaml --config environment.yaml --output <run-directory>`（目录须不存在）。其中：
  - 原始载荷与账本：`run.json`、`ledger/`、`case.json`、`load.json`、`density.json`、`validation.json`、`target_bindings.json`、`steady.json`、`execution_journal.json`、`resource_samples/`、`host_samples/`、`provider_evidence/`、`fixture_restore/`、`source_lifecycle/`、`cleanup.jsonl`。
  - 最终封存证据：`<run-directory>/evidence/`（`case_manifest.json` + 各 `*.json`、`summary.json/csv`、可选 `summary.xlsx`、`raw/…`），可摘要复算。
- 图内 worker：会话目录由**部署层**通过 `configured_session(case, config, folder)` 注入（通常为工作/临时目录），仓库里没有默认位置。

### 2. 不可变产物库（artifact store）

- `FileArtifactStore(root)`：内容寻址，布局 `<root>/<scope>/<sha256>`（blob）+ `<root>/<scope>/<sha256>.json`（`EvidenceRef` 清单）；`artifact_id=<scope>:<sha256>`。
- 谁写入：
  - `delivery_finalizer`（`sandbox_bench/bridge.py`）在发布交付前把 N10 封存证据**全部文件**入库，`scope=group_id`，并生成 `archive_index` / `case_index_ref`。
  - 分析产物（`facts.json`、`report.md`、`facts.xlsx`、`comparison_manifest.json`）由 `AnalysisReportServices` 以 `scope=run_ref` 入库。
  - 原始编排日志只以引用形式出现（`store_raw_log` 校验 `artifact://` 引用），不内联进控制状态。
- **`root` 由部署层提供，仓库无默认值/env**；根 `.gitignore` 忽略 `/artifacts/`。

### 3. 节点上有什么

- 三产品**环境组节点**上只有运行时事实、运行时原始日志及远端 journal/受限备份（环境配置的 `remote_agent`/`journal`）；**它们不是交付件仓库**。
- 测量证据由 orchestrator 侧的会话/采集器组装并封存，再经 `delivery_finalizer` 入库。

### 4. 会不会同步回工程目录

- **不会自动同步进源码目录**。要让产物出现在工程目录，只能把 `--output` 或产物库 `root` 指到工程目录（并被 git 忽略）。
- 取回方式：
  - 分析产物（含 Excel）：`python -m sandbox_graph export-analysis --run-id <run> --artifacts-root <root> --out <dir>`。
  - 某 run 的测量证据（数据/原始载荷/资源样本）：`python -m sandbox_graph export-cases --run-id <run> --artifacts-root <root> --out <dir>`（每个 work 一个子目录，内容为 N10 封存并经 `delivery_finalizer` 入库的同一份证据，即 A3 的输入）。
  - 用例证据：按交付输出里的 `case_index_ref` 在 store 中定位 `<scope>/<sha256>`（scope 为环境组）。
  - F4 被阻塞时，F1/`analysis_exports` 仍可定位产物；这只证明产物流水存在，**不证明交付或覆盖完整**。
