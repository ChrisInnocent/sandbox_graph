# 受限角色团队与现代模型诊断

本入口复用真实 `TaskBackend` / `Worker`、`LifecycleBudgetService`、`LifecycleContextService` 和 `AgentBridge`。它不调用当前聊天宿主的 dispatch agent，也没有进程内 spawn 队列。默认未装配；健康执行和可用的确定性修复不调用模型。`LifecycleSupervisor` 只有在已注册规则没有可用动作时，才把诊断交给持久 investigator job。

## 可调用的装配

先由可信边界登记 `RepairPolicy`、exact scope 授权和原 lifecycle/lineage 预算，再明确提供 provider 与模型配置。`cost_unit` 必须为 `microunits`，并与 policy、局部及 lineage 预算一致；对应 provider 协议的 `cost_microunits`，没有汇率、价格或隐藏默认额度。

```python
from sandbox_graph.services.agent_team import AgentTeamService, ModelTeamConfig
from sandbox_graph.services.lifecycle_context import LifecycleContextService
from sandbox_graph.runtime.agent_bridge import HttpAgentProvider

context = LifecycleContextService(repository, artifact_store,
    artifact_scope=evidence_scope, secret_values=registered_secret_values,
    max_read_bytes=max_read_bytes, total_evidence_bytes=total_evidence_bytes,
    max_packet_bytes=max_packet_bytes, max_source_bytes=max_source_bytes)
config = ModelTeamConfig(schema_version=1,
    provider_id=provider_id, model_id=model_id, model_version=model_version,
    max_input_tokens=max_input_tokens, max_output_tokens=max_output_tokens,
    max_cost_microunits=max_cost_microunits,
    max_cancel_cost_microunits=max_cancel_cost_microunits, cost_unit='microunits',
    deadline=absolute_model_deadline, max_observations=max_observations,
    poll_seconds=poll_seconds, max_input_bytes=max_input_bytes,
    max_output_bytes=max_output_bytes, evidence_bytes_per_grant=evidence_bytes_per_grant)
team = AgentTeamService(repository, context, config=config)
provider = HttpAgentProvider(explicit_bridge_config)
handlers = build_job_handlers(..., lifecycle_bindings=exact_lifecycle_bindings,
                              model_team=team, agent_provider=provider)
worker = Worker(repository, handlers, policy=explicit_worker_policy)
```

上例中的变量必须来自操作员明确配置；省略号表示已有 graph/adapter 装配参数，不能直接复制执行。`HttpAgentProvider` 的 `BridgeConfig` 仍要求明确 endpoint、provider、bearer token、精确 scope、网络 timeout 和请求/响应大小。一个配置的 provider 必须覆盖该 job 的实际 scope；多个 work 通常需要分别装配 scope 固定的 provider/worker，不能扩大一个 provider 的 scope 来绕过授权。

`model_team=None` 继续已有确定性行为。提供 `model_team` 但缺少 `lifecycle_bindings` 或 `agent_provider`，bootstrap 直接拒绝装配。缺失 provider capability 时，只产生一条有界 role job 和明确 blocker，不自动创建替代任务。现代调用不会回退到旧 `AgentGateway` 的 caller 字段字典或 caller 布尔处置状态。

## 角色任务与公开服务接口

| 接口 | 作用和授权边界 |
| --- | --- |
| `AgentTeamService.request_role(parent_job, *, token, role, candidate_ref=None)` | 供可信 supervisor 调用。`parent_job` 必须是实际仍有 claim 的 `lifecycle` / `stage=diagnose` job；token 必须同时拥有其 thread、原 lifecycle 和共享 work 锁。只允许 supervisor → investigator/coder/reviewer；不接收 caller depth 或 context 字典。返回稳定 task/job/provider request ID。 |
| `AgentTeamService.query(task_ref)` | 供已认证的应用内部查询。返回冻结 contract、原 request ID、input digest、状态、观察轮数、reason 和 proposal/decision/result 引用；不返回 packet、证据内容、完整 envelope 或私有会话，不触发网络。此 Python service 不是外部未认证 API。 |
| `AgentTeamJobHandler.observe(job, *, token)` | 供持有原 role job、thread、lifecycle 和 work owner 的 Worker 调用。消耗原持久观察额度；既有 unknown 只 lookup 原 request ID，known handle 查 status；已收集 terminal replay 直接读本地账本。 |
| `AgentTeamJobHandler.cancel(job, *, token)` | 供相同真实 owner 调用，取消原已知 handle；先持久同原 ID 的一次性 intent 并预留独立 safety stop/cost。拒绝或未知结果重放只读原事实，不再次远程取消。unknown 没有 handle 时拒绝猜测目标；accepted 不是 terminal。 |
| `RoleContract`、`TeamProposal` | 严格 wire 模型，冻结角色、parent/task/lifecycle/source/scope/graph/plan/contract/candidate/model/version/digest 身份。proposal 只携带结构化事实/证据引用、假设、已注册动作或 candidate-needed 等路线。 |

现有 `LifecycleSupervisor` 是受信 supervisor 角色，子 role job 是 `agent_team`。默认只开放一层实际关系，即使 policy.max_depth > 1，也不能把 role job 作为父任务递归派发。当前没有更深层 delegation 授权/版本接口；增加此能力需要后续显式可信授权，不能依据模型或 payload.depth。policy.max_depth 与 max_concurrency 都是严格正值，缺配置不排队。

contract 绑定 parent job、node supervisor、原 lineage budget、run/scope/node、原 source receipt、graph/plan/contract、revision、候选和固定模型版本。队列 payload 只保存引用和冻结版本。新 role/context/worker 不会建立新 lineage，也不会刷新原 deadline。相同 parent/role/candidate 重放复用同一 job；相同身份改模型或额度直接冲突。

## 事务预算、上下文与模型权限

排队的同一事务核验真实 parent/job/thread/lifecycle/work owner、policy、lineage 并发和深度，并提交 contract、原输入摘要、预算预留、job 与 outbox。investigator 同时预留 `child_jobs=1`、`diagnosis_calls=1`、input+output 最大 tokens、最大 cost 与本任务全部 `max_observations`；coder/reviewer 当前只预留 child job，因为工具依赖尚未接通，不发模型请求。局部和 lineage 上限同时验证；即使底层预算配置更宽，仍拒绝超过当前 policy 的子任务/调用/token/观察限制。额度不足或排队事务中断不留下半个 job 或单边预算。

观察额度预先占用并保守按配置总轮数消耗，不在成功后退还未用观察轮次；实际轮数另存于 task.observations。模型 tokens/cost 在可验证 terminal receipt 提供用量时使用协议 usage 结算，缺 usage 或状态无法核实时扣满原预留，计数类额度不回退。v1 negative lookup 不构成未执行证明。新的 role 或新 worker 不能清零轮次。

输入仅由 ContextService 生成的可信 packet 与其已授权脱敏 evidence grant 范围组成。每个 grant 至多读取配置的 `evidence_bytes_per_grant`，还受 context 单次/累计字节限制；读取范围按原字节入账，packet 失败事实不截断。超 input/output 字节限额直接拒绝。父会话、coder 私有会话、其他 node 私有证据、路径、任意文件/脚本与可执行工具均不在接口中。reviewer 的 packet 从原 lifecycle 重新构造，只引用同一可信 candidate 身份，不复制 coder 会话。

模型 schema 禁止未知字段，无法提交 guards、budget、实际节点结果、脚本、递归派发或自签候选门禁。source receipt 事实必须逐字匹配可信 packet 的原事实或错误摘要；grant 事实必须引用本次实际读取的 handle，并逐字出现在该范围中。这是可核验的文字摘录，不把模型的解释当成已证实事实；解释进入独立 hypotheses。

`registered_action` 还必须属于原 policy，精确匹配已注册 action/version/node 且可用。模型不直接下单：proposal、费用结算、原 source failure 绑定的 decision 和 `decision_ready` 在同一实际 job fence 事务提交；后继 supervisor 仍通过现有 `consume_decision` → `request_remediation_in`，重新验证 exact 授权、work revision、资源处置、gate、动作权限及修复预算。`LifecycleService.record_decision_in` 只是原 record_decision 的事务内等价接口，原 wrapper 保留。

## Provider 恢复与尚未实现的依赖

网络在事务外进行。Bridge 在 submit 前持久原 envelope/request identity；每次实际 provider 调用前重新检查真实 ledger job、与该 job 相同的 thread token、lifecycle 和 work fence，返回结果在发布前再次检查。网络与模型结果发布只允许 active investigator，且必须存在与冻结 contract/envelope/usage ceiling 精确绑定、尚未结算的模型预留；blocked coder/reviewer 无法通过公开 observe、Bridge 或提交入口绕过。submit/lookup/status 的实际调用在网络边界消耗同一持久观察轮数，直接使用 Bridge 也不能避开限额。远端成功但本地尚未提交时崩溃，后继 Worker 使用原 Bridge result/handle，不换 request ID，不重复扣 tokens，不重复创建 proposal/decision/order。

远端 observation 达上限时，known handle 在安全预算允许时最多发出原请求的一次取消；unknown 无 handle 不猜测取消目标。没有 children_stopped 的 terminal receipt，持久 task 状态为 `waiting_provider`、job 为 blocked，保留原 request ID 和 lineage 并发槽位，费用按最大预留结算。取消 accepted 仍属于此状态。正常 Worker 超时、尝试耗尽或派发取消，也由完成 watchdog 写具体 blocker 并保守结算。不会无限派替代 job；原团队执行入口不自动扩额或跨 job 恢复 exhausted role。下文新增的可选安全消费者沿原 request ID 和独立已授权 safety 预算处理，不能重发新请求。

取消费用配置 `max_cancel_cost_microunits` 明确必填且为正整数，没有默认零费用。一次取消先原子保存 `agent_team_cancellations` 与 `stop_calls=1`、明确最大 cost 的局部/lineage safety 预留，同时校验 policy 上限和 microunits 单位。真正取消 I/O 前把 intent 标为 unknown/network_started；即使 provider 返回 accepted=False、丢失确认或进程在 I/O 前后中断，同 job 重放也只返回原事实，不再次远程调用。v1 cancel 没有可信费用用量字段，所以按整个显式预留结算；未完成结算由 job completion watchdog 补齐，模型结果先成功提交也不退回取消费用。accepted 仍需后续 children_stopped 才构成停止证明。没有安全额度时不发取消，也不创建免费 intent。跨 job 继续取消必须由另外明确授权的安全续办处理，不能重入本普通团队执行入口；可选实现见下文链接。

这是本批新配置的严格必填项；缺少该字段的旧冻结任务不会获得隐式费用或网络权限。其处置需要明确受信配置/恢复流程，不以默认值迁移。父 job deadline 也从真实 ledger 读取；构造 context 后在最终排队事务重验全部权威 deadline，期限缩短时拒绝旧合同，contract/envelope/job 始终使用同一冻结期限。

后续增加了可选 [原 ID 有限安全消费者](lifecycle-reconciliation.md)，填充上文原团队子交付未实现的跨 job 续办。waiting_provider 会保存原 request ID 的 provider_reconciliation_required；明确装配服务和独立安全授权后，新安全 job 只经窄 safety_target 合同查询/取消原 Bridge 身份，确认 terminal+children_stopped 才追加终结事实并关闭原槽位。普通 observe/cancel 的旧 owner/预算限制不放宽，不创建新模型、role 或 decision，也不因迟到 usage 退款。未装配仍保留依赖 blocker。该子交付不补 coder/reviewer 工具、生产策略切换或现场 sampler 停止能力。


`candidate_needed` 由可信运行时依据已核验 proposal 生成不可变 `candidate_needed_intents`，冻结 source/lifecycle/lineage/版本/提案摘要。对应 outbox 在未装配消费者时保留未 delivery，`outbox_errors` 明确 `waiting_dependency: candidate_tools_46_not_connected`。当前 coder/reviewer 只能保存与已有 candidate intent 身份相符的独立角色合同；实际执行明确 blocked、零 provider 调用，不能报告真实编码、隔离构建、测试、独立代码评审或候选门禁成功。46.* 尚需实现隔离 workspace、工具后端、候选版本与测试/评审结果消费者。

本地定向测试使用真实 Worker/TaskBackend/Bridge/ContextService 和真实文件 evidence store，provider 是明确的受控外部协议夹具。它不等于真实模型服务、宿主 agent、生产沙箱、PostgreSQL 多进程崩溃或真实计费验收。
