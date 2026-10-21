# 编排实施报告

2026-10-21，实施范围为 `sandbox_graph/`、`tests/orchestration/` 与本目录文档。代码已写入共享工作区，未提交；本报告不声明整个 OpenSpec 变更完成，也不把内存夹具测试视作 PostgreSQL 或真实产品验收。

## 已运行的验证

环境：HarmonyOS，Python 3.14.7；LangGraph 1.2.16、checkpoint 4.3.0、checkpoint-postgres 3.2.0、psycopg 3.3.6、Pydantic 2.14.1、pytest 9.1.1 已实际导入并运行。正式 PostgreSQL 目标版本仅是配置目标，尚未连接服务端验证。

本轮阶段性命令与实际结果：

| 命令/阶段 | 实际结果 |
|---|---|
| `python -m pytest tests/orchestration -q`，组交付完成后 | 71 passed, 3 skipped in 6.74s |
| 同范围，服务重跑/处置/封存检查加入后 | 81 passed, 3 skipped in 7.59s |
| 队列超期、默认装配/F2、复核修复、混合计划、终态处置、封存真实在途、stage-only、清理环预算、T2→T6 | 每项先运行失败断言，再实现并重跑通过；没有跳过失败用例 |
| 正式 PG 测试 | 无 `SANDBOX_GRAPH_TEST_POSTGRES_DSN`，3 项明确 skipped，未计通过 |

最后一次完整范围及跨组件结果见本报告末尾的验证追加。根代理统一执行全库和 OpenSpec 严格校验；本报告只复述自己实际取得的输出。

## 当前可执行路径

- 严格身份/状态模型、45 节点目录、有限选择、依赖与能力缺口、同摘要预览/启动；混合计划仅阻塞缺合同的 case。
- 正式 PostgreSQL Repository/UoW 与 epoch-fenced 官方 saver 适配已经实现。业务控制事务使用全局 advisory lock；saver SQL 与接管锁同一 thread lease 行。没有生产 SQLite 或内存回退。
- 持久 job/claim/receipt、outbox/inbox/cursor、owner 续租、资源预约与 quarantine、精确 activation 机器等待、人工 interrupt/decision 恢复；超期和 superseded 会产生终态事件。
- Worker 有界领取、执行、续租、完成、outbox 消费与 watchdog；GraphJobHandler、ResumeJobHandler、RecoveryJobHandler 和固定版本 `build_job_handlers` 可调用。未知 handler 持久 blocked。
- 注册脚本 intent→许可+submission_started→外部提交→对账；响应丢失后复用原 operation，不自动重发未知副作用。取消和资源清理分别记录。
- 真实五类 LangGraph；N00–N10 的各失败出口仍进入停止/清理/交付。Driver 支持当前 token 工厂、原版本恢复、精确 decision；结束只释放 thread lease，资源域持续保留。
- T6 重读原文件并追加复核；成功摘要和 unverified 数据不能通过。证据损坏再修复会追加新 review。T8/GE 发布版本化组交付，冻结子 work/review；R3 只消费组交付，其他组未完成也可生成增量分析。旧分析继续读取原冻结版本。
- T6 只对终态 work 记录明确处置；缺交付保留 insufficient/unknown。GE 保留 group disposition、精确 gaps、unsupported/N/A。Closure 在最终写事务检查 fence、计划、revision、实际任务/操作/资源域；unknown 仅在明确 partial+quarantine 时允许部分归档。
- A/F 使用根代理实现的实际 `sandbox_bench.workflow_services` 与 comparison/Excel/report 服务；接口和运行证据见 [analysis-report.md](../analysis-report.md)。本包不制造计算结果以通过图测试。
- reanalyze/rerender 及 stage-only 指定精确源 run/input，校验归属，用独立线程派发纯阶段，不启动测量。

## tasks 1–14 的验收建议

以下“可勾”指该任务的当前代码、合同和本地适用验收已具备；要求真实数据库或完整组合故障的任务不在其列。根代理应结合最新全库结果最后核对。

| 任务 | 建议 | 依据/仍缺部分 |
|---|---|---|
| 1.1 | 不勾 | 安装和真实图已跑；正式数据库最小连通未跑，平台支持矩阵未建立 |
| 1.2–1.3 | 不勾 | 严格模型/分离状态/CAS/有限重试有测试；设计全模型、单位/时钟/重试矩阵尚未逐条完整覆盖 |
| 1.4 | 不勾 | FakeClock、内存事务、受控runner有；T02完整三组oracle/SpyModel与全部fixture provenance矩阵未齐 |
| 2.1 | 不勾 | 45节点与方法目录齐；全目录S/M/H责任/逐节点验收矩阵未齐 |
| 2.2 | 可勾候选 | `unit/test_planning.py` + `test_services.py`：并交排除、空/未知/授权、同hash启动与混合缺口；有限示例见setup |
| 2.3–2.4 | 不勾 | 条件/准备依赖/能力/stage-only和图校验可用；复杂依赖DAG/有效证据语义、完整图CRUD/版本激活矩阵未齐；带required_cleanup的预算环保守拒绝 |
| 3.1–3.2 | 不勾 | 正式UoW、visit/receipt/intent已实现；真实PG与checkpoint两侧硬杀未验 |
| 3.3 | 不勾 | 文件原子发布/range/篡改/缺文件已测；文件成功数据库失败orphan组合故障尚未专测，无GC服务 |
| 3.4 | 可勾候选 | `graphs/test_group_delivery.py` + `unit/test_review_closure_agents.py` + runtime event tests：v1冻结不被v2/新review污染，精确组/子交付与review版本 |
| 4.1–4.3 | 不勾 | 所有权/资源域/fenced saver代码及本地合同有；真实双连接事务顺序、子图/pending writes硬杀恢复未验 |
| 5.1–5.4 | 不勾 | 任务/事件/wait/decision/watchdog及Worker本地断言有；完整barrier竞态、通知丢失、owner硬杀与PG持久interrupt组合未齐 |
| 6.1 | 可勾候选 | `unit/test_runner_protocol.py` + `test_models.py`：注册脚本、结构化输入/hash/secret引用、先intent/提交标记、unknown不重发，公共Protocol已文档化 |
| 6.2–6.5 | 不勾 | 独立runner进程响应丢失、观测段/取消清理/透传可用；controller+PG硬杀、全部deadline/迟到清理/measurement合同组合还缺 |
| 7.1–7.4 | 不勾 | 五真实图+Worker+独立组/work+增量交付已接；45节点逐项矩阵、持久saver恢复、R6已接有界analysis.recompute:1，外部supplement/remeasure执行器与完整返工矩阵未完成 |
| 8.1 | 可勾候选 | `graphs/test_environment.py`及五阶段Worker测试：auth/unknown/未验证拒绝、注册C2、独立C3、共享域互斥/unknown隔离；真实安装脚本证据仍另验 |
| 8.2–8.4 | 根代理核对 | BenchSession/bridge实现由根代理维护，见benchmark集成；controller硬杀恢复仍未验，不能只凭本包路由测试整条勾 |
| 9.1–9.3 | 不勾 | 有界上下文/结构化提案/预算/usage unknown有；存储权限完整矩阵、语义review协议和实际资源处置联动未齐（目前调用方传resources_disposed） |
| 10.1 | 可勾候选 | `graphs/test_group_delivery.py` + `unit/test_review_closure_agents.py`：WorkDelivery→T6→GroupDelivery、原文件重读、拒收/修复追加、gap与终态处置保留 |
| 10.2–10.4 | 不勾 | 实际A/F计算/导出/报告和独立重跑已接；A5已接同一分析重算执行合同；外部返工、全方法不可比/单位与五阶段增量端到端矩阵尚未全部验收 |
| 11.1–11.5 | 不勾 | impact、gate/permit、谱系CAS、reuse候选与调度门控有；accepted复用交付投影与producer绑定已接；正式PG激活竞态、旧wait撤销、旧代码保留/回退检查未齐 |
| 12.1 | 可勾候选 | `unit/test_services.py`、`test_cli.py`、`test_worker.py`、`graphs/test_driver.py`：共享服务/最小CLI、幂等命令、精确resume、stage-only与同hash；retry只入队，未知执行器明确blocked |
| 12.2–12.3 | 不勾 | usage与账本有；统一trace/阶段耗时/复用计数及从空真实PG启动五阶段未交付 |
| 13.1 | 可勾候选 | [requirement-coverage.md](requirement-coverage.md)逐条WF/ENV/RT列模块、测试与实际缺口，不将本地测试冒充ENV/RT实测 |
| 13.2–13.3 | 不勾 | [script-contracts.md](script-contracts.md)与分层边界已写；完整节点验收样例/旧任务入口同步、可执行里程碑归档检查尚未齐 |
| 14.1–14.4 | 不勾 | 正式PG组合故障未跑；双组合成五阶段已通过；人工等待/纯阶段组合端到端、有限种子属性/变异、ORC001–070逐例证据矩阵与最终一致性验收尚未全齐 |

## 明确剩余及实施简化

1. 正式 PG 没有服务/DSN，真实竞态/断连/controller硬杀是环境阻塞；不能用当前内存fixture替代。
2. R6/A5现执行显式注册的analysis.recompute:1，单order至多一项既有analysis Worker任务；supplement/remeasure与仅A7重渲染缺对应执行器，持久记录blocked而不排无人消费job。完整返工矩阵尚未交付。retry命令仍属根服务的独立执行策略，不能把本次分析返工等同测量重跑。
3. accepted候选已自动投影为新work交付，保留原身份/原raw并重验producer measurement binding和精确review。分析按原delivery+case hash去重并保留新scope aliases。缺binding/policy不可默认复用，等候超期策略仍需调用方显式选择blocked或remeasure；真实PG复用竞态未验。
4. 条件展开支持Case显式method_version与严格正整数repeat_id，默认均为1，已测试轮次/方法版本不碰撞；measurement_generation的追加执行服务仍需扩展。没有复杂依赖DAG自动计划器。
5. GraphService保存历史不可变定义，但尚无自动跨版本代码装载/安全保留检查。bootstrap固定版本且拒绝不匹配版本；部署必须保证原版本worker可用。
6. PostgreSQL采用统一JSONB账本和全局控制事务锁，尚未做按记录物理约束/吞吐优化；所有远端执行都在事务外。SQL正确性仍须正式后端验证。
7. FencedSaver限定官方3.2.0当前使用的put/put_writes/get/list同步/异步API；尚未验DeltaChannel历史接口、复杂子图和库升级兼容，不声明通用所有saver扩展已通过。
8. 模型接口是受限Protocol；真实模型连接、语义复核、部署认证/租户鉴权不在现有本地测试证据中。AgentGateway资源处置由调用方显式证明，未接自动处置后诊断流程。
9. 文件产物有不可变提交与范围读取，无对象存储/可达性GC；实验资源清理不能删除原始证据。
10. 真实安装/配置、三产品环境合同、权限、阈值及Guest净值证明仍由实际接入提供。没有真实L5数据，不能声称任何产品性能得到验证。

本报告与任务清单之间存在未完成事项是当前事实；不应归档主变更，也不应把“未验收”统一解释为仅缺 PostgreSQL。

## 最后验证追加

```
.venv/bin/python -m pytest tests/orchestration -q -rs
83 passed, 3 skipped in 6.26s
```

三项跳过均为 `tests/orchestration/persistence/test_postgres.py` 的真实数据库验证，原因完全相同：`BLOCKED: SANDBOX_GRAPH_TEST_POSTGRES_DSN not provided; real PostgreSQL required`。未使用替代数据库伪造通过。

```
.venv/bin/python -m pytest tests/benchmark/integration/test_analysis_graph.py tests/benchmark/integration/test_work_graph_session.py -q
4 passed in 4.09s
```

后一个命令实际运行根代理维护的真实LangGraph、BenchSession/bridge、公式/Excel/报告组件组合；数据、Repository/saver仍为显式合成夹具。它证明这些调用链可运行，不是完整多worker/PG恢复或真实产品数据验收。


## 五阶段 Worker 集成追加

新增 `tests/benchmark/integration/test_five_stage_worker.py`，显式合成远端Environment/Scenario，真实执行 Worker、五类LangGraph、E1/E2/C1/C2/C3、RegisteredBenchmarks、BenchSession N00–N10、版本化GroupDelivery、A/F统计与Excel和报告。两组分别有5个成功请求、5个失败请求；断言事实分层、不可比、无排名，资源清理未知只产生partial归档。首次通过：`1 passed in 19.97s`，随后加强逐值断言并进行联合复测。

该测试暴露并修复两个真实集成断点：R3读取交付时同时记录事件cursor，R7引用同次R3 receipt，避免从0重复唤醒；GraphJobHandler在交付发布前核验job claim，并将未核验清理的预约域转quarantine。只有清理明确完成/不需要、所有operation终态且资源均closed时才释放预约。没有将unknown cleanup改成complete。

Case还支持显式environment_ref；同组多个非空环境引用冲突拒绝，group job与work record保留声明值；缺失不会猜测环境。

配置共享域追加修复：C1从冻结Plan预约域并绑定run_id，C2核验域所有权，只能对账已存在的quarantine操作；C3独立probe满足且原配置operation已明确完成，才在同一释放事务再次核验operation并释放。unknown/复检失败保持quarantine。资源释放outbox按Plan域交集向相关组发布事件，覆盖等待登记前后两种顺序，避免共享域冲突组永远停在GW。正式PG并发事务顺序仍待验。


配置租约/资源唤醒后的联合复测：

```
.venv/bin/python -m pytest tests/orchestration tests/benchmark/integration/test_five_stage_worker.py tests/benchmark/integration/test_analysis_graph.py tests/benchmark/integration/test_work_graph_session.py -q -rs
94 passed, 3 skipped in 32.66s
```

其后补充WorkGraph编译时缺N09适配器必须拒绝的RED→GREEN断言；缺清理handler不能等到已有副作用后才发现。最终计数以下一条追加为准。

最终相同联合命令（含WorkGraph编译守卫）：`95 passed, 3 skipped in 28.67s`，进程退出码0。三项skipped仍全部为真实PostgreSQL环境缺失。此后仅更新本报告与交接消息，没有代码修改。

最终真实性门控追加：Case未声明部署能力时默认`unverified`/`adapter_verified=False`，编译结果blocked；所有支持路径合成fixture显式声明supported/True，CLI示例同样标明合成声明。负例先失败后通过。最终相同联合命令为 **96 passed, 3 skipped in 37.18s**，退出码0；三项跳过仍为正式PostgreSQL无DSN。该结果后的修改仅此文档追加，实施文件冻结。

## 后续有限块：accepted 复用与 R6/A5 分析返工

在上一次冻结后按授权继续两个明确控制路径，不能沿用上一段“此后仅文档”的时间界限：

- 复用由候选门控推进到 producer binding → 原 review 冻结 → 原始文件复核 → 新范围引用交付 → 独立 review → group/work delivered。严格核验实际 measurement contract hash 与注册 script hash；模板引用名称相同但参数变化会拒绝。G0 在 T2 绑定前保持 pending，T5 自动投影；源 review 晚到经 outbox 通知精确新 group。accepted review 更新不会改写首次冻结的 review 引用。
- 新交付不复制旧 EvidenceRef，不重写原样本身份。A2 和分析物化独立验证投影；根代理的真实分析集成测试验证两个新 work 引用一份原样本，事实/Excel coverage 保留 aliases，样本计数仍为一份。
- 根桥接层仅对显式部署 reuse policy、真实 case/script binding、succeeded/valid 交付封存 reuse_contract。没有 binding/policy 的历史交付不自动变成可复用；没有实际 L5 数据证明。
- typed ReworkOrder 与 `analysis.recompute:1` 已由 R6/A5 接到已有 AnalysisGraph Worker，预算一次、事务去重、精确计划/输入/受影响范围，并在完成 outbox 留 `rework_results`。根图携明确 rework_ref 时 R2 不派发测量组。专项测试实际计算并导出 Excel；未知 supplement/remeasure action 直接保存终态 blocked，不留下无人消费的 job。

TDD 实际输出：自动投影/A2 篡改检测首次 `2 failed, 2 passed`；review 更新不应破坏冻结引用首次 `1 failed, 5 passed`；typed rework 模块首次 `3 failed`；根返工不得先派发测量组首次 `1 failed, 4 passed`。均先出现目标失败，再实现并复测。最后专项和根组件联合为 `24 passed in 8.94s`，随后增加真实 Worker 返工/R2 守卫，并以末尾最后联合输出为准。

公共合同与精确记录字段见 [reuse-rework.md](reuse-rework.md)。这些改动缩小 WF11、7/10/11 组任务的缺口，不构成整个任务组完成：只注册了分析重算，外部补测/重测与仅 A7 的局部重渲染执行器仍未交付；候选超期策略仍由明确调用选择；PostgreSQL 并发/硬杀及完整计划激活/回退矩阵仍未验收。原任务可勾候选列表不因本轮而自动扩大。

本轮最后联合验证（实现与测试均冻结后）：

```
.venv/bin/python -m pytest tests/orchestration tests/benchmark/integration/test_five_stage_worker.py tests/benchmark/integration/test_analysis_graph.py tests/benchmark/integration/test_work_graph_session.py -q -rs
118 passed, 3 skipped in 39.14s
```

进程退出码 0；三项 skipped 均为真实 PostgreSQL DSN 缺失，未使用内存替代。此输出之后只补充本段及交接消息，没有修改实现/测试。全仓最终输出由根代理单独持有和归档。
