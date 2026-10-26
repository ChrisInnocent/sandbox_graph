# 统一实施与验证记录

2026-10-24 后续新增 [helper 初始化与离线投递记录](agent-lifecycle-runtime/bootstrap-20261024.md)；单独列明本地安装与真实 SSH 验收边界。

2026-10-24 最新停止缺陷修复及离线回归见 [R1–R3 修复记录](agent-lifecycle-runtime/stop-fixes-20261024.md)；此前的 [端到端检查](agent-lifecycle-runtime/e2e-review-20261024.md) 保留发现问题时的快照。两份记录区分局部修复、代码交付缺口与真实环境验收。

2026-10-22 生命周期首批增量的独立记录见 [首批验证记录](agent-lifecycle-first-batch/README.md)。下文保留原日期对应的历史结果，不代表当前增量的全库验收。

2026-10-23 后续的历史兼容、规划检查和实际测试收集见 [检查维护记录](agent-lifecycle-runtime/verification-maintenance-20261023.md)。该记录明确列出已收集范围与缺失设计路径，收集成功不代表完整 G1 或全库执行通过；最新实现边界见 [实施记录](../orchestration/progress.md)。

2026-10-21，OpenSpec change：`sandbox-evaluation-workflow`。用户已授权设计完成后直接 apply。本轮已生成设计、测试契约及96项实施任务，并实现下述代码。整个 change 尚未完成，不归档、不宣称真实候选产品性能已验证。

## 可复查的验证证据

冻结代码的最终结果：**435 passed、4 skipped、101 subtests passed**，76.73秒，退出码0；测试期间被测代码与测试文件无变化。任务整条验收完成 **23/96**，其余为部分实现或未验收，不把通过的测试数量换算成需求完成率。

最终回归原始输出见 [pytest.txt](pytest.txt)，逐测试结果见 [pytest.xml](pytest.xml)。命令：

```bash
.venv/bin/python -m pytest -q -rs --junitxml=docs/verification/pytest.xml
openspec validate sandbox-evaluation-workflow --strict
.venv/bin/python scripts/verify_planning.py --output docs/verification/checks.json
.venv/bin/python -m compileall -q sandbox_graph sandbox_bench
git diff --check
```

全库包含既有草稿测试；未隐藏基线缺失 runner 导致的失败，现已补实现。并行开发中的 RED 和中间回归不是最终证据，以上文件只保存最后一次完整运行。`checks.json` 记录文档链接、任务编号、节点目录与当前包版本检查；`source-manifest.json` 记录最终代码、测试和依赖文件摘要。

实际本机为 HarmonyOS、Python 3.14.7。实际导入版本：LangGraph 1.2.16、checkpoint 4.3.0、checkpoint-postgres 3.2.0、psycopg 3.3.6、Pydantic 2.14.1、pytest 9.1.1、openpyxl 3.1.5。锁文件记录安装版本，不把未运行的其他 Python/OS/PostgreSQL 版本当成已验证。

## 已交付的可运行链路

- 五类真实 LangGraph，主统筹／环境组／工作节点边界，45节点目录；有限选择、状态账本、注册脚本、租约与 fencing、等待和精确恢复接口。
- N00–N10 BenchSession，三产品受控协议适配，B01–B10基础方法与E01–E06低优先级注册骨架。WINDOW、BATCH、有限开环、实际完成终点和持久 parent/child 预算共用执行器。
- 密度双增长口径、投影双公式、固定Q、创建吞吐，PSS／CPU／稳态／实际cohort分母和Guest减项证明。缺失数据不补零，估算不冒充实测。
- B08独立恢复预算与未知操作对账；单宿主共享来源引用、源恢复、明确所有权清理、独立不存在证据和迟到封存检查。
- 实际统计、横向可比性、逐轮结果、Excel、事实报告和部分归档；处置绑定run/plan，原始证据重算后再做最终CAS。
- 新图新运行、旧图收尾；经生产者合同和参数摘要验证的复用只建立引用。新工作覆盖和原始样本分别记录，同一原测量不会重复计数。
- 双环境组合成远端的完整Worker五阶段，以及大原始日志、零模型测算、局部硬退出与多类负例。它们运行真实图、公式、Excel和文件证据，但仓储及远端夹具明确标为合成。

## 已勾选任务的证据索引

下表中的测试均纳入最终全库。勾选表示整条任务在其声明范围内完成；其他未勾任务可能已实现部分代码，不能据此推导已完成百分比。

| 任务 | 实现与主要验收 |
| --- | --- |
| 2.2 | `planning/compiler.py`；`tests/orchestration/unit/test_planning.py`、`test_services.py` |
| 3.4 | `services/reviews.py`；组交付冻结版本及 `test_review_closure_agents.py` |
| 6.1 | `runtime/runner.py`；`test_runner_protocol.py`、`test_models.py` |
| 8.1 | `services/environment.py`；`graphs/test_environment.py`、双组五阶段测试 |
| 10.1 | 分层原始证据重读、审阅追加和组交付；`test_group_delivery.py` |
| 12.1 | `interfaces/service.py`；服务、CLI、Worker与Driver测试 |
| 13.1 | [逐要求覆盖账本](../orchestration/requirement-coverage.md)，区分WF/ENV/RT与真实验收 |
| 16.3、16.4 | `completion.py`、`logs.py`；四终点及Kuasar原始日志关联负例 |
| 18.1–18.3 | `load.py`；WINDOW/BATCH/开环独立黄金序列，`test_journal_kill.py`三硬退出切点 |
| 21.3 | 密度投影16/18/14黄金值、窗口与部分健康反例 |
| 22.3、22.4 | 实际cohort／稳态断段／覆盖与GuestProof；`test_steady_windows.py`、`test_metrics_resources.py` |
| 23.1–23.4 | 逐请求分位数、吞吐尾桶、9核峰值／25MiB均值、不可变封存与离线重算 |
| 24.1、24.2 | `extensions.py`受控profile与补偿骨架；未选零执行、未知动作及期限负例 |
| 14.4、26.4 | 最终全库、严格OpenSpec、链接／节点／任务检查和本分层交付记录 |

## 仍需实现的部分

这些缺口不是由“没有真实环境”统一解释，也没有从 tasks 删除或改成已完成：

1. B02共享一次build后K次创建、共享成本归属和完整build接管阶段机。
2. B08产物大小及完整来源副作用证明；snapshot/image删除和物理释放的完整产品适配。当前来源删除只允许明确证明的template能力。
3. 远端命令task身份与未知执行对账；跨主机FIRST authority。文件锁实现只承诺明确的单宿主共享文件系统边界。
4. R6/A5已注册重算可运行，补样／重测等未注册返工动作仍明确阻塞；完整measurement_generation、自动跨版本代码保留／回退检查尚未交付。
5. 完整trace／阶段耗时／复用计数、诊断前自动资源处置联动、全量ORC/BTC属性／变异与故障组合矩阵。
6. 具体部署的探测、密钥配置、安装升级脚本和inventory/collector部署，不由通用环境handler的受控测试证明已交付。

编排细节见 [编排实施报告](../orchestration/implementation-report.md)，测试执行边界见 [运行配置](../benchmark-wiring.md) 和 [场景合同](../benchmark-scenarios.md)。

## 实际环境阻塞

2026-10-23 生命周期增量的[本机后端预检](agent-lifecycle-runtime/backend-preflight-20261023.json)记录了工具发现和实际 namespace 启动命令：bubblewrap 0.13.0 的启动能力检查失败，不能在本机宣称候选隔离/G4 已验证。只读根挂载仅用于无副作用启动探针，不是候选部署配置。

- 3项真实PostgreSQL测试：未提供 `SANDBOX_GRAPH_TEST_POSTGRES_DSN`，没有以内存或SQLite替代通过；多worker、双连接fencing、断连和controller硬杀仍需正式后端验收。
- 1项真实Linux procfs测试：本机为HarmonyOS；冻结proc树测试不代替真实内核、权限和多节点采集验证。
- 未提供三产品真实环境连接、部署版本及对应实验合同，没有运行真实功能／性能／密度测试。阈值、安装策略、Guest净值定义等未决输入只阻塞相应实验，不填入合成默认值。

上述实施验收结束时，代码和文档保留在工作区，未提交或发布；后续Git提交和公开发布由用户另行授权，不代表OpenSpec change已完成或已归档。


## 环境适配 change 阶段提交状态（2026-10-21）

新增 OpenSpec change `implement-sandbox-environment-adapters` 正在实施，当前 **22/65 tasks** 勾选。此次状态单独记录，不改变上文 `sandbox-evaluation-workflow` 的 23/96 状态，也不将公共底层实现计作三产品适配完成。

最新全库验证命令 `.venv/bin/python -m pytest -q`：**521 passed、4 skipped、101 subtests passed**；严格校验命令 `/storage/Users/currentUser/.npm-global/bin/openspec validate implement-sandbox-environment-adapters --strict` 通过；`git diff --check` 通过。输出在本次工作区终端中验证，未生成 pytest XML 或原始日志归档。

这次实现的范围是环境声明/SSH/主机探测/凭据/证据/配置事务/修复计划/有限参数/远端 journal/执行网关/注册动作及通用 inventory 等底层组件。**三种产品的产品级探测、安装与配置修复、环境图与 CLI 接线，以及真实 Linux/PostgreSQL/产品验收均未完成。** 工作机为 HarmonyOS，未提供真实环境连接信息；4 个 skipped 不得解释为真实环境测试通过。任务清单保持未勾选状态，后续提交会继续更新本记录。

## 生命周期运行时增量回归（2026-10-23）

[编排层离线记录](agent-lifecycle-runtime/runtime-interim-20261023.md)：1692 passed、19 skipped，保留实际日志、JUnit及178份源摘要；这是阶段快照，不是完整门禁或真实后端验收。

- [本地sampler与原ID安全续办阶段回归](agent-lifecycle-runtime/sampler-interim-20261023.md)：编排层1748 passed、19环境相关skip；不代表全库或完整生命周期门禁。
