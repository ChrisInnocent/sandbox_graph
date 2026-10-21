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
