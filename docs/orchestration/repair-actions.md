# 修复动作目录

本目录实现 ALD-03/04/06 的版本化白名单和 45 个持久节点的策略登记。目录及受控执行路由的状态为 **locally_verified**；现场资源、Runner 后端、宿主适配和持久并发未在本任务验证，不标记 backend_verified 或 adapter_verified。

## 解析与执行

```python
from sandbox_graph.catalog.repair_actions import standard_repair_catalog

catalog = standard_repair_catalog()
action = catalog.resolve('resource.cleanup', '1', 'N09').require_available()
manifest = action.input_schema.model_validate({
    'schema_version': 1,
    'source_receipt': 'receipt:failed',
    'evidence_refs': ('raw:failure',),
    'input_refs': {'contract': 'contract:1'},
})
# 可信服务先完成范围、授权、证据来源、资源处置和预算检查，并持有真实租约。
# executor 必须是配置了 N09 handler 的 NodeExecutor。
result, receipt_ref = await action.executor_route.execute(executor, state)
```

模块级 `resolve(action_id, version, node_id)` 使用同一默认目录。动作、版本和节点必须都是明确的字符串；版本不匹配、跨节点借用动作、未知动作或代码片段均抛出 `RepairActionNotRegistered`。已规划但未实现的动作能解析为 `unavailable`，调用 `require_available()` 抛出 `RepairActionUnavailable`。

`NodeExecutorRoute` 固定绑定登记节点，只接收真实 `NodeExecutor`；缺少该节点的可调用 handler 时，在创建 visit/receipt 前拒绝。执行沿用真实 executor 的 fence、节点复审和新 receipt 路径。传入的 `result_refs` 继续保留，不改写原 receipt 或原始证据。目录不按动作名动态 import、eval 或执行 shell。

`RepairActionInput` 继承严格生命周期模型：显式整数 `schema_version=1`、非空 `source_receipt`、至少一个 `evidence_refs`、字符串引用组成的 `input_refs`。未知字段和代码字段拒绝；Python 调用以 tuple 传证据，JSON 数组经 `model_validate_json` 解析。schema 验证只证明形状；失败证据真实性、hash、归属、授权、scope 和输入绑定仍必须由持久服务验证，不能把引用字符串视为权限。

## 登记的动作

| 节点 | 动作与版本 | 状态 | 效果类别 | 所需 capability | 恢复边界 |
|---|---|---|---|---|---|
| N01 | case.reconcile@1 | available | external_read | operation.read_owned | 已知原操作状态后编译恢复计划 |
| N02 | fixture.repair@1 | unavailable | external_mutation | fixture.repair_owned | 重验 N01/N02/N03 |
| N03 | preflight.recheck@1 | unavailable | external_read | preflight.read_owned | 不放宽原断言 |
| N04 | observation.repair@1 | unavailable | external_mutation | collector.repair_owned | 完整新 baseline；不拼接断段 |
| N05 | resume_owned_operation@1 | unavailable | external_mutation | operation.resume_owned | 仅原 operation；有效性能 fail 不触发补测 |
| N06 | steady.reobserve@1 | unavailable | external_read | observation.read_owned | 原 sampler 连续且合同未改变 |
| N07 | evidence.seal@1 | available | artifact_write | evidence.seal_owned | 新 manifest 引用原 spool；不制造事件 |
| N08 | analysis.recompute@1 | unavailable | artifact_write | analysis.write_revision | 冻结 raw，新结果 revision |
| N09 | resource.cleanup@1 | available | external_mutation | resource.cleanup_owned | 清理本人资源后重验被销毁的依赖 |
| N10 | delivery.republish@1 | unavailable | artifact_write | delivery.publish_owned | 幂等交付，不增加样本 |

所有动作登记 `work_owned_resources` 冲突域，服务须将其绑定到具体 work 的共享互斥键；目录不自行取得锁或分配预算。`available` 表示已有固定节点执行路由，运行时仍需注入相应 handler。现有 `services/remediation.py` 的旧白名单和调用路径保留，应用服务接入目录由其他任务完成。

## 后验、对账与清理

`action.postcheck(result, review, *, disposition=None, checked_at=None, operation_statuses=None, collection=None)` 首先要求执行成功、无 error、结果未被拒绝、独立复审存在且节点匹配、复审决定为 accept。缺复审、错节点或 unavailable 动作均返回 false。通用 NodeAgent 的 accept 不能替代以下动作专属证明；省略相应证明也返回 false。

| 动作与后验引用 | 必需证明与拒绝规则 |
|---|---|
| N01 / case.known_operation@1 | 非空 `result.operation_refs`，以及完全匹配这些原 operation ID 的 `operation_statuses: Mapping[str, RunnerStatus]`。每个状态必须 authoritative，且为 not_submitted/completed/failed/cancelled；unknown、running、非权威或错 operation 均拒绝 |
| N07 / evidence.complete_collection@1 | `result.data_status='valid'`、非空原证据 `artifact_refs`，以及 `RunnerCollection`。收集产物集合须与结果完全一致，interrupted 必须为 false，missing_sequences 必须为空；有效执行不能掩盖缺样或无效数据 |
| N09 / resource.closed_disposition@1 | `result.cleanup_status='complete'`、服务已核验的 `ResourceDispositionReceipt` 和数值 `checked_at`。证明须由 lifecycle_safety@1 发布，时间在 observed_at（含）与 expires_at（不含）之间；复审 owner 必须属于同一 work；stopped 为 true、admission closed、sampler stopped/not_started，无 residual 或 isolation，operation 和资源 inventory 集合与结果完全一致 |

调用方必须在真实租约保护的事务中，从绑定此次 receipt 的可信账本取得复审和证明，并验证证明来源、归属及当前事实。N09 可复用 `LifecycleSafetyService.verify_disposition_in` 的返回对象；`checked_at` 使用该事务的当前时间。N01/N07 同样须先核验原 operation 与 collection 的 scope、身份及账本来源。这些参数不能从模型或 `result.output` 构造为“已验证”事实。目录检查动作所需语义，服务检查证明的真实性；新的恢复计划和节点验收仍由后续流程承担。

目录中的 hook 引用是控制合同名，不是动态 Python 路径：

| 引用 | 现有受控服务或处置合同 |
|---|---|
| runner.reconcile_owned@1 | `runtime.runner.OperationService.reconcile(operation_id, scope, token)`，查询原 operation 并校验 fence/所有权；unknown 不得重新 submit |
| runner.collect_owned@1 | `runtime.runner.OperationService.collect(operation_id, scope, token, last_seq)`，按序收集原始记录并核对缺失事件 |
| runner.cleanup_owned@1 | `runtime.runner.OperationService.cleanup(scope, resource_ids, token)`，仅清理归属匹配的资源并记录残留 |
| evidence.preserve_raw@1 | 原始证据保留约束；不提供删除 raw 的执行能力 |

未实现动作的专用后验、对账或清理引用仅声明目标合同，尚不可启用。目录登记这些引用不自动运行 hooks，也不构成后端验证证明。worker 必须使用原 operation、真实租约和具体资源清单调用相应受控服务。

## 45 个节点策略

`catalog.node_policies` 是只读映射，每个 `NodeRepairPolicy` 含节点、owner role、scope kind、明确 policy version、带版本动作引用、修复可用性和不可用原因。analysis/report 继承 run scope；不借用 work 的现场操作 capability。

| owner role | scope kind | 节点 |
|---|---|---|
| run | run | R0、R1、RP、R2、R3、R6、R7 |
| group | group | G0、T0、T1、T2、T4、T5、T6、T8、TC、TB、GB、GW、GE、E1、E2、C1、C2、C3 |
| work | work | N00–N10 |
| analysis | run | A1、A2、A3、A4、A5、A7 |
| report | run | F1、F2、F4 |

N01/N07/N09 可用；其余节点明确为 unavailable。无具体动作的节点有空动作集合和 `no_registered_repair_action` 原因。全部节点允许进入诊断和候选演进的策略判断；这两个声明仍须满足实际授权、provider 能力及预算，不授予无限执行许可。数值预算和绝对期限必须由运行合同中的 `RepairPolicy` 提供，目录没有无限或猜测的默认额度。

## 本地验证范围

`tests/orchestration/unit/test_repair_catalog.py` 的 68 个用例覆盖三元组解析、45 节点策略、未实现动作拒绝、严格 schema、真实 NodeExecutor 路由、缺 handler 时零 visit/receipt、原证据保持、独立复审和动作专属证明反例。真实 NodeAgent 接受的 unknown/incomplete cleanup、无效封存数据、未知 operation 仍被目录拒绝。隔离进程中绕过动作专属检查的变异导致 10 个真实 NodeAgent 路径用例失败，原代码保持不变。对应 ALU-01/03、ORC-077/078 中目录边界的部分；完整提案证据来源、跨 scope 授权及持久入队由后续服务测试验收。
