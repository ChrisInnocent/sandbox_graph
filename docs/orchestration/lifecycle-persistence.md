# 生命周期持久记录与修复请求

`services/agent_lifecycle.py` 用不可变失败 receipt 确定 lifecycle_id；相同失败重放复用原记录，不能换 policy 重置历史。工作、环境组和 run 的身份来自 ledger，实际线程租约和作用域任务共同限定当前 owner。状态事件有 event_id、expected_revision 和证据引用，使用 CAS 提交；`decision_ready` 与决策在同一事务保存。消费决策的回调只允许事务内账本写入，外部动作必须成为持久任务。

`services/remediation.py::request_remediation` 在同一个事务内核验实际 owner、冻结 work revision、当前派发 generation、未撤销授权、已登记动作、失败与输入证据、共享 work 锁及处置凭证；然后预留 lifecycle/lineage 预算、分配 attempt、写 order/job/outbox 和状态事件。同 request_id 同内容复用，异内容拒绝；输入与前置 result_refs 透传，不从模型提案生成任意命令。

旧 `order()` 仍提供明确的查询式拒绝结果，但不能再发起新任务：有效动作提案返回 `blocked/lifecycle_contract_required`，恢复入口为 `request_remediation`。历史 remedy_orders 和预算不重写，旧 worker 兼容测试从显式历史记录开始，不能把旧提案当作新合同授权。

## 原图与修复的共享互斥

Driver 对已登记的 work 获取 `work_execution_leases`，逐节点执行与 receipt 发布均复核实际线程及 work owner。开始新 attempt 时使旧收尾事实失效；只有图结束且同 activation 的真实 N07/N09/N10 receipts 齐备，才登记 attempt_closed。采样状态读取 observation ledger，节点返回成功不能覆盖仍运行的 sampler。中断/取消只释放执行所有权，保留资源、操作及隔离记录。

Worker 完成回写会增加 work revision；仅当关闭事实属于该实际完成 activation 且原 revision 一致，才同步元数据 revision。迟到完成不能把另一 attempt 的事实改为已关闭。修复留下 `repair_isolations` 时，即使没有 operation ID 或资源域，也禁止普通执行/恢复，并禁止生成已闭合的处置凭证；受限诊断/清理/对账仍可获取执行所有权。

## 归档闭合检查

`ClosureService.seal_archive` 在归档事务中先收集当前 run 的 work、group、operation 与 lifecycle 作用域，再检查隔离、provider、sampler 和共享执行所有权。当前 run 的 cleanup/reconcile 与修复、候选任务处于 pending/running/draining 时均禁止归档；analysis/report（含 graph: 别名）属于封装归档的发布任务，不作为此类资源处置阻塞项。另一 run 的任务和 operation 不扩大当前 run 的作用域。

同名 group 仅用于发现 sampler/resource lease 候选。Sampler 通过 `operation_id` 对应 operation 的 run 消歧；operation scope 与 sampler scope 或明确 work/run 身份冲突时，不能据此排除记录。Resource lease 优先采用显式 `run_id`；该身份与唯一 work/run scope 归属冲突时拒绝归档。明确且一致的其他 run 记录不阻塞当前归档；本 run 的运行中 sampler/live lease、当前作用域内缺失归属或绑定矛盾的记录仍保守检查，不删除或改写原记录。原有 quarantine 加显式 partial 资源处置规则保留，未知归属不能据此生成 clean archive。

Provider 请求优先采用关联 lifecycle 的 run 身份；已知属于另一 run 的请求不会因同名 group 阻塞本次归档。缺少 lifecycle 的旧记录按 scope kind 检查：run/work 采用明确身份，当前 group、operation 或显式提交的作用域仍保守阻塞；同名 group 无法证明归属时不视为已完成。裸 node 名称不代表全局所有权，node 请求须关联当前 run/work、当前 operation 或当前 node lifecycle 的作用域。未知响应和未停止的 provider 子任务仍禁止归档。

保留 repair isolation 的显式 partial 归档要求该作用域已作 partial 处置，且所有绑定该 marker 的当前 run lifecycle 均为同作用域的 blocked_closed；无关的 fulfilled 历史问题不会否决此处置。缺失或不匹配的绑定、任意未终止 lifecycle 仍被拒绝。归档保留 marker 引用与各 lifecycle 状态，不删除隔离或改写性能结果；fulfilled 仅表示生命周期终止，不能把性能 fail 改为 pass。

## 验证边界

当前有 MemoryRepository 事务回滚、竞争、fence、真实 StateGraph/Worker 的定向验证；不是 PostgreSQL 双连接、硬杀、真实 runner 或宿主验收。现代动作执行见 [修复执行](repair-execution.md)，预算见 [双层预算](lifecycle-budgets.md)，处置证据见 [安全服务](lifecycle-safety.md)。完整监督调度、RecoveryPlan、模型授权/预算接线、候选演进仍以 OpenSpec 43–49 的未完成任务为准。
