# 编排公共接口与边界

所有 JSON 模型拒绝未知字段、非有限浮点；顶层模型 frozen。嵌套 dict/list 不提供深层不可变保证，持久入口重新校验或序列化，调用方不得原地修改已发布数据。`schema_version=1`；规范化摘要为 UTF-8/sorted keys/无空白 JSON 的 SHA-256。所有时间与截止须来自明确时钟合同；数据库租约用数据库 wall time。

## 数据合同

`domain.models` 公开 `RunRecord`、`WorkKey`、`WorkInstance`、`NodeAttempt`、`Operation`、`ScriptRef`、`EvidenceRef`、`DecisionRecord`、`DeliveryManifest`、`NodeResult`、`ErrorRecord`、`TimePoint` 和 `canonical_hash`。身份、单位和字段见相应严格模型；WorkKey 的 repeat_id/generation 与节点 attempt/visit 分离。Operation input_hash 必须匹配结构化 input，凭据只接受 secret:// 引用，注册 ScriptRef 必须完全匹配 ID/version/digest。

NodeResult 六个状态独立：execution、data、review、performance、cleanup、coverage。性能 fail 不会触发重新测量直到通过。终结 execution 不允许复活；重试创建新 attempt。`transition(result,event,expected_revision,revision)` 检查 CAS 输入和合法状态边；`merge_revision` 保留新版本并拒绝同版不同内容；`retry_allowed` 只允许有界 TRANSIENT_READ。

## 计划、图与服务

| 接口 | 合同 |
|---|---|
| `Registry.resolve_node/resolve_method/register_handler`、`standard_registry` | 45 工作节点、B/E 方法目录；所有产品适配默认 unverified，注册目录不证明适配完成 |
| `PlanCompiler(catalog).compile(selection, authorized_groups, evidence, graph_ref, contract_ref)` | 同轴并集/跨轴交集/最后排除；显式空、未知、零匹配区分；只用显式条件，不生成笛卡尔积；B04 准备依赖不是 B01/B03 性能样本 |
| `Plan.hash`、`RunService.preview/start` | 预览保存不可变计划，启动验证原摘要、权限和证据；变更需新预览；命令 request_id 幂等 |
| `RunService.status/resume/cancel/retry/reanalyze/rerender` | 查询或持久排入命令；返回 queued 不代表 worker 已完成；取消关闭普通派发，清理另行确认 |
| `QueryService.status/versions` | 返回账本事实和精确历史版本；接口面向受信控制面，部署层还需用户/租户认证与查询范围检查 |
| `GraphService.get/list/propose_patch/validate/diff/activate_revision` | 未知 handler、无界环、固定/动态重复边、清理旁路拒绝；定义历史不可变；激活必须提供候选计划 |
| `planning.changes.diff` | 文字→F，布局→A7/F，阈值→A3起，规格/未知语义→重测；存在新事件仍需语义/时间域复核 |
| `RevisionService.create/permit/activate_revision` | request_id、predecessor CAS、gate、permit、谱系/outbox同事务；新图新run/thread，同图新plan CAS；旧许可与新普通许可明确分开 |
| `ReuseService.consider/can_measure/expire` | 已验收且合同/环境/方法/事件/时间域/有效期一致才写 reuse_from；不改旧work；候选有期限，超时需blocked或remeasure处置 |

调度器在资源预约前处理复用候选：waiting/accepted/blocked 不重复发压，只有显式 remeasure 可进入测量。accepted 候选在真实producer绑定、源权限、原始文件复核通过后自动生成新范围引用交付；不复制原始样本、不增加样本数。完整公开字段、独立验证入口及 R6/A5 有限返工执行合同见 [reuse-rework.md](reuse-rework.md)。

## 持久化与执行

| 接口 | 合同 |
|---|---|
| `PostgresRepository.migrate/transaction`、`UnitOfWork.get/put/scan/delete` | 同事务 revision CAS 和 immutable 检查；业务记录在统一 JSONB 表以 kind/key 唯一；无生产内存后端 |
| `UnitOfWork.acquire_thread/assert_fence/renew_thread/release_thread` | thread lease 行锁、递增epoch；同事务核验 owner_epoch/lease_id/期限 |
| `FencedPostgresSaver` | 绑定不可变 token；put/put_writes/async variants 在真正SQL事务内锁租约并验证；无pipeline延迟提交；worker禁止setup/delete/copy/prune |
| `LeaseService.acquire_thread/renew/acquire_all/quarantine/release` | 域排序、全有或全无；实际资源预约不随driver超时释放；release需要已核验闭合 |
| `TaskBackend.enqueue/claim/finish` | activation去重、有界claim、epoch防旧写、结果receipt/outbox；可扫表找回，不依赖内存通知 |
| `EventService.append/consume` | event ID及aggregate seq冲突检测；缺序等待；消费projection、inbox、cursor同事务；producer scope允许集必填 |
| `WaitService.park/wake/accept/watchdog` | park与事件recheck同事务；版本特定唤醒；答案校验问题/计划/图/interrupt/期限；超时只发布资源处置outbox，不批准 |
| `Driver.start/resume` | 一次activation输入固定；租约heartbeat、有限执行片段；checkpoint恢复原图、receipt先行；构造器明确要求saver factory，支持取得真实 token 后的handler_factory；interrupt保留waiting，decision精确恢复；完成后只释放thread lease |
| `NodeExecutor.execute` | 本节点最小NodeContext，无messages；visit持久分配、loop新visit、同输入receipt重用；handler返回NodeResult，缺handler明确blocked |
| `build_work` | 真实StateGraph N00–N10；失败进入N07/N09/N10；状态仅小型引用与状态摘要 |
| `build_run/group/analysis/report`、`build_definition` | 真实StateGraph、注册handler、明确router；有界循环独立visit和执行预算；没有未注册业务动作的占位成功实现 |
| `build_decision_graph/resume_decision` | 独立无副作用interrupt节点，精确待决interrupt ID和scope/version恢复；必须通过resume helper核验ID |

`WorkflowHandlers.mapping` 提供主图、组统筹和部分A/F控制动作；`run_definition/group_definition/analysis_definition/report_definition`及对应 routers 是版本1拓扑。组/分析/报告图必须注册剩余真实服务才可编译。`SchedulingService.dispatch_groups/expand_work/dispatch_work` 持久派发独立逻辑任务，测量并发与每工作请求并发分开。Case.method_version（默认1、非空）与repeat_id（默认1、严格正整数）参与WorkKey；多轮由不同Case显式声明，未选轮次不会隐式展开。measurement_generation追加服务仍未实现。

## Runner / 环境 / 观测

`RunnerGateway` 为异步 Protocol：

```python
submit(operation: Operation) -> RunnerStatus
reconcile(operation_id: str, scope: str) -> RunnerStatus
cancel(operation_id: str, scope: str, reason: str) -> RunnerStatus
collect(operation_id: str, scope: str, last_seq: int = 0) -> RunnerCollection
cleanup(scope: str, resource_ids: tuple[str, ...]) -> CleanupResult
```

RunnerStatus.state为not_submitted/running/completed/failed/cancelled/unknown；authority来自authoritative字段；取消不代表清理。RunnerCollection有events、last_seq、missing_sequences、artifact_refs、interrupted；序列缺口须显式报告。CleanupResult分别给出complete/incomplete/unknown和remaining。

`OperationService.submit/reconcile/cancel/collect/cleanup`持久化intent→permit+submission_started→外部动作→事实。submission_started之后unknown只对账，不默认重发。预算和指标原样透传，不在图层重算或填0。远端是否识别fence仍是gateway的责任；当前Protocol没有通用远端fence RPC，不能承诺外部恰好一次。

`EnvironmentProbe.connect/inspect`返回带scope/source_ref/认证/身份/state/differences的EnvironmentSnapshot；`EnvironmentHandlers`提供E1/E2/C1/C2/C3。真实探测程序需注入；未验证配置适配不执行，check-only不执行C2，C3独立重读实际状态。

`ObservationService.start/append/restart/stop`保存独立采样会话operation、段时钟epoch和事件序列。重启产生interrupted，不合并成连续稳态；缺号是insufficient，不是0。真实采样器看门狗与日志spool由执行端负责。

## 证据、模型、审阅与收口

`FileArtifactStore.commit/verify/open_range/path_for`：分块写临时文件，fsync、硬链接原子发布、独立manifest；摘要/尺寸/作用域核验；open_range默认64KiB预算。实验资源清理不删除原始证据。对象存储、引用扫描与GC尚未实现。

`AgentGateway.build_context/diagnose/validate_proposal`：allowlist投影，输入/输出字节、调用数、token和时间预算；事实必须引用允许证据，建议只能是注册动作；越scope和额外metric字段拒绝。调用前要求resources_disposed；模型响应usage缺失记unknown。没有模型参与确定性成功路径。`ModelGateway.diagnose`返回proposal、usage或None、model_version。

`ReviewService.review_delivery/freeze_analysis/get_analysis`重读原始文件、追加ReviewRecord、保存精确delivery版本和review引用；被拒数据仍保留。规则级方法语义检查需额外验证器，当前结构/身份/文件核验不证明方法正确。

`ClosureService.classify_scope/seal_archive`要求处理记录、scope_disposition、无在途、同版analysis/Excel/report与closure CAS；unknown cleanup只能partial。`seal_archive(token, expected_plan_ref, ...)` 在最终写事务重查fence、activeplan、真实work/config jobs、operations及资源域；running阻止归档，uncertain仅在本scope已partial且对应域quarantine时允许partial。实际F4范围/产物核对由sandbox_bench.workflow_services提供，真实数据库竞态尚未验收。

## Worker、层次交付和终态处置

`Worker(repository, handlers, policy).run_once/serve/pump_outbox` 有界领取、续租、执行与完成；outbox与机器等待可重扫。过期/预算耗尽/superseded均发布完成事件。`GraphJobHandler` 在Task.finish前完成manifest发布；`ResumeJobHandler`恢复原activation的原graph handler；`RecoveryJobHandler`实现受scope约束的reconcile/cleanup，unknown保持隔离。未知handler不会伪成功。

`build_job_handlers` 的公共关键字：graph_ref、saver_factory、node_service_factory、review_service、deadline、lease_seconds、segment_seconds，另可设置owner、max_work_concurrency、delivery_finalizer、operation_service。工厂仅接受所声明graph版本。WaitRecord必须冻结activation_id；唤醒不以排序或latest推断原activation。

T6写不可变review和scope_processing，并投影scope_dispositions[work_id]。规则terminal-review:1仅处置终态工作：真实成功+valid+accepted为fulfilled，失败为failed，取消为cancelled，其余已终结缺口为blocked_closed。该规则仅允许带缺口报告，不把unknown cleanup变成complete，不释放资源。missing manifest保留insufficient/unknown。

T8冻结子work delivery/review；GE写group_deliveries[id:version]、group_delivery_sources及group_dispositions，保留所有子版本与gap。R3只消费group_deliveries，按每组精确版本冻结analysis_inputs（delivery_kind），不混用work delivery。`ReviewService.resolve_analysis`返回manifest及groups，每组含delivery、review、work_deliveries、work_reviews、source。重复同语义复核幂等；证据损坏后修复会新增review，不复活旧review指针。

F2可以从输入或本activation精确F1 receipt读取facts_ref/analysis_ref；跨分析引用拒绝。A/F真实计算/Excel/报告接口见 [analysis-report.md](../analysis-report.md)。

Case.environment_ref可显式声明；同组多个非空引用冲突拒绝，Scheduler将唯一引用传group job且写入work record。缺失环境引用由E1明确blocked。R3/R7共享同次冻结event_cursor，避免读取历史事件后重新从0等待。交付发布对真实job claim做epoch/期限检查；资源预约按实际cleanup/operations/resources保留或隔离，不能因job结束直接释放。

配置步骤C1按冻结Plan预约conflict_domains，LeaseService.acquire_all可选owner_run_id阻止跨run同名scope复用锁。C3独立复检且明确completed operation才允许release(operation_refs,owner_run_id)，operation核验与释放同事务。release发布resource_domains_available outbox，Worker按Plan域交集持久通知相关group，登记等待前发生的释放同样可被recheck读取。WorkGraph编译要求全部N00–N10 handler存在，缺清理adapter提前拒绝。

Case的部署默认值为capability=unverified、adapter_verified=false；单独填写方法目录和条件不能解锁真实执行。只有显式已核验声明可进入受支持路径，合成测试也必须显式声明。

## 节点生命周期合同（2026-10-22 增量）

纯模型与事件转换已分离到 [lifecycle-contracts.md](lifecycle-contracts.md)。这些类型描述 phase/CAS/预算/停止原因/恢复引用，不代表持久生命周期服务、授权或模型接入已经完成。正式调用者须在事务内核验数据库事实并构造 guards，模型的布尔声明不能直接作为 guards。

RemedyJobHandler 的 job claim 与 thread lease 分别校验；线程 lease_id 必须来自仓储分配。任务失去 claim 或线程失去 fence 后，禁止写 node receipt、修复 outcome 或继续任务；取消/异常仅释放仍归自己的线程租约。独立 NodeReview.reject 与 NodeResult.review_status=rejected 都触发监督记录，原 NodeResult 不改写。

生命周期服务的持久化、事务派发及原图互斥实现见 [持久记录说明](lifecycle-persistence.md)，现代修复执行见 [动作执行说明](repair-execution.md)。这些模块的存在不代表 43–49 的完整循环或真实后端验收已完成。
