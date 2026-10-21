# 后续业务脚本接入清单

本清单是当前编排实现的业务接入边界，替代旧 `align-sandbox-benchmarks` 的泛化重复审计入口；不修改旧变更历史或表示其任务完成。已有源码结论继续使用 [source-audit](../source-audit/alignment.md)，不重新安排无目标的全仓审计。

| 节点 | 输入/输出与关键合同 | 当前缺口/验证责任 |
|---|---|---|
| E1/E2 | EnvironmentProbe.connect/inspect(scope,environment_ref) → EnvironmentSnapshot；实际来源、认证、身份、角色、状态、差异 | 通用路由已测；真实SSH/服务探测、角色配置和采集权限由具体已核验适配提供 |
| C1/C2/C3 | C1引用实际E2快照并绑定已核验ScriptRef；C2 OperationService；C3独立重读生效状态 | 真实安装/密钥/配置命令与回滚尚未交付；没有verified适配不会执行 |
| T2 | 适配协议、完成事件、真实规格、采样能力和固定预算 → NodeResult断言与缺口 | 需注册每方法适配预检；缺方法不能用exit=0替代 |
| N00–N03 | WorkSpec/合同/源身份 → 输入、对账、fixture、就绪验证 | root维护BenchSession/bridge；prepare不自动扩成完整性能矩阵 |
| N04–N07 | 独立observation operation与pressure operation；baseline→操作→后验/稳态→stop/seal | 预算、Q_total/C/B/gap、完成事件与clock epoch由原合同透传；accepted不是complete；分段不能拼接连续稳态 |
| N08 | 原始artifact refs与规则版本 → 有效性、局部统计 | 图不重算第二套公式；必须接实际benchmark统计，缺规则/单位blocked |
| N09 | 所有权inventory、scope、已授权策略 → complete/incomplete/unknown+remaining | 取消确认不代表清理；unknown隔离；不删其他scope资源 |
| N10 | 工作身份、所有尝试、raw、统计、六状态、gaps → DeliveryManifest | Worker需要output.delivery_manifest或async delivery_finalizer；仅成功文本/文件路径不足以成为可验收交付 |
| T6/T8/GE | work_deliveries+ownership → 追加review、组summary、事件 | T6支持失败/缺清单gap；GE发布版本化GroupDelivery及精确child/review引用；R3只读GroupDelivery并增量冻结分析 |
| A1/A2 | 精确AnalysisInputManifest → 冻结版本与原文件重验 | 已有核心服务；不能读latest污染正在执行的版本 |
| A3/A4 | 已验收raw、比较条件、计算版本 → 带单位/来源的统计、可比性/不可比理由 | 必须注册真实统计/比较服务；单候选不造排名，缺合同不填默认 |
| A5 | 有证据的rework_order → 明确recompute/supplement/remeasure范围 | 当前校验/引用已实现；自动生成完备返工与新attempt执行服务需补齐 |
| A7 | 精确analysis版本 → 真正Excel工作簿、公式/单元格证据索引 | sandbox_bench.workflow_services调用实际comparison/Excel服务；A4导出，A7发布report job；缺adapter拒绝编译 |
| F1/F2/F4 | 精确分析、事实引用、覆盖/资源/原始文件 → 报告、核验、archive | F2版本检查和closure核心已实现；sandbox_bench.workflow_services已提供真实事实装配、报告产物和F4核对；PG竞态/完整端到端仍待验 |

## Worker 与 BenchSession 接口

```python
handler_factory(token: LeaseToken, activation: Activation) -> dict[str, AsyncNodeHandler]
async node_handler(context: NodeContext) -> NodeResult
async delivery_finalizer(job: dict, graph_result: dict) -> DeliveryManifest
```

将最终清单scope设为实际group_id或work_id；同一清单中的artifact scope须与该访问作用域匹配。`work_refs`必须包含job.scope_ref（实际work_id），`plan_ref`必须等于冻结job.payload.plan_ref，contract_ref匹配WorkInstance。Scheduler工作payload含work_ref、group_id、plan_ref、graph_ref。

GraphJobHandler在Driver真实执行结束、Task.finish之前调用finalizer并验证清单，随后写 `work_deliveries[delivery_id:version]`、`work_delivery_owners`。T6据ownership查本组工作，重读ArtifactStore并保存ReviewRecord；缺清单的终态工作登记gap。finalizer必须保留原执行失败和unverified/insufficient数据状态，不能为了通过验收改成valid。

`Worker`的registry示例：run/group/work/analysis/report映射GraphJobHandler；cleanup/reconcile映射RecoveryJobHandler；resume映射ResumeJobHandler。GraphJobHandler必须使用原graph_ref对应的builder/handler工厂；图定义缺失要blocked，不能把版本1强当任意版本。retry等业务命令需要显式注册有范围、有限预算的实现，当前无默认“整轮重测”handler。

Q1/Q2/Q4–Q8/Q10–Q12仍属于真实合同输入。特别是Q12 Guest OS净值扣除，缺同域、内容、大小和来源证明不得输出净数；环境/规格/负载/调优/阈值与真实权限不能由合成fixture推断。

验收交接分层：L0纯逻辑；L1真实LangGraph；L2真实PostgreSQL；L3真实controller/runner硬杀及账本核验；L4完整五阶段合成端到端；L5三产品真实接入。ControlledRunner的独立进程文件账本只验证该夹具自身响应丢失与身份，不替代L2/L3组合验收。
