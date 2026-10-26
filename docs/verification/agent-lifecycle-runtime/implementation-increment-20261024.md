# 2026-10-24 实施增量

本轮按“实现全部非环境阻塞项”继续实施；本记录是进度证据，不是任务完成声明。

工作目录已从 `.worktrees/agent-lifecycle` 合并到项目根。233 个文件逐文件校验后复制；原有根目录文件备份位于工作树 `artifacts/integration/20261024/root-before`，摘要清单见根目录 `artifacts/integration/20261024/from-worktree.json`。三个上游源码目录保留。没有提交、推送或修改 Git 索引。

已验证增量：

- 完整用例文件/矩阵生成、真实 session 参数和来源摘要：37 项相关测试；tasks 51 已完成。
- 五种创建后 HOLD 变体、独立资源窗口、实际就绪分母、先停采样后删除及原始证据封存：92 项相关回归。真实产品验收待环境。
- sched_switch 已接 N04 attach/pre-roll、N05/N06 trace 窗口、N07 stop/join/detach 和 N10 归档；丢事件或清单变化只给不完整/下界，精确峰值为空。时钟与 sampled 分列。
- 产品参数规则、首批安装 recipe、文件/systemd 只读探针、宿主锁/CAS/原请求执行器、报告投影与 auth_ref 已有代码；完整环境图、配置及角色注册/鉴权修复链仍在接线。
- 候选从指定 Git commit 建独立工作区，不导入用户脏文件；编辑路径/CAS、冻结修订、保守风险分类和 Bubblewrap 后端已有代码，11 项专项通过。
- 门禁策略、受信 producer 和 lease 证明、候选/目标/环境/套件绑定、完整 case 集及过期/撤销校验已有服务，相关模型与反例91项通过。

各回归集合有重叠，不相加。工作树组合173项通过；合回根目录后的定向组合178项通过，JUnit：`artifacts/reviews/implementation-20261024/root-integration.xml`。全量回归另跑，不能用该组合替代。

环境探测：本机未找到 postgres/initdb/pg_ctl，未提供专用 PostgreSQL DSN。Bubblewrap 已安装，但实际 `bwrap --unshare-all --new-session --die-with-parent --ro-bind / / -- /bin/true` 返回 `Unexpected capabilities but not setuid, old file caps config?`（exit 1）。没有将单元测试登记为真实隔离通过；原 stop-persistent 的14个PG/硬杀用例仍因缺DSN blocked。产品真实部署与模型 provider 验收分别保留。

## 根目录后续实现

- `EvolutionService` 冻结 proposal/target/base、校验允许路径和授权、按 lifecycle/lineage 预留 revision 预算，原请求幂等；中断只核验原编辑结果，不重放编辑。
- `CandidateWorker` 使用现有应用内 AgentBridge 接收结构化 CAS 编辑；控制侧固定门禁验证实际冻结文件。受控测试实际执行 r1 修改失败→r2 修改通过；响应丢失只查询原请求。`EvolutionCoordinator` 已通过 TaskBackend 持久排队下一次迭代，并接入 bootstrap 和 lifecycle 的 needs_code 分支。缺作用域配置保持 waiting_dependency。
- `TrustedGateRunner` 在候选解释器外运行登记用例、核验完整收集、源文件摘要及 producer，保存逐用例观察和 GateManifest；候选输出不能自报通过。标准 G0–G6 profile 的真实套件/JUnit/隔离部署装配仍需继续补齐。
- `CandidateReviewService` 保存独立身份/上下文分配及绑定 gate 的复核；`PromotionService` 在单事务核验 gate、review、发布授权、冻结的后继 plan/contract/auth 和 worker，执行 revision CAS。缺 ready worker 不关闭旧 gate；候选不能调用旧激活入口绕过；TaskBackend 领取和执行前均核验新 run 的 bundle/图版本。完整 review provider、不可变 bundle 制作、CLI、取消和主 lifecycle 状态事件接线仍在继续。
- 上述候选/调度/门禁/晋级/旧 revision 回归组合 **66 项通过**：`artifacts/reviews/implementation-20261024/candidate-chain.xml`。这是受控协议和本地文件验证，不是已调用宿主 dispatch 或真实隔离验收。
- 首次根目录完整回归 **2824 passed / 1 failed / 34 skipped / 110 subtests passed**，JUnit 为 `root-regression.xml`。唯一失败为采样停止测试依赖固定 10ms sleep；已改为等待原采样任务和 executor join，其 17 项专项通过。后续实现后仍需重新全量验证。
- 产品侧补齐：资源 generation 跨环境组单调；AgentENV scheduler/gateway 按真实容器 image ID/RepoDigest 检查，native 安装解包早于账户 home 创建；Cube 固定安装根、package/依赖锁逐个摘要检查、只允许字面量环境文件；每个 recipe 步骤持久记录成功/失败/未知。Kuasar admission rate 依据本地源码确认为 int，拒绝小数。
- `InstanceTokenVault` 私有保存数据面 token/route key，绑定 scope/instance/generation、保留版本和删除墓碑；Adapter create/info/resume/fork/execute/delete/cluster ownership 恢复已接入。对应鉴权/适配回归34项通过；删除后旧内存 token 不能绕过墓碑。
- 环境报告同时扫描引用的原始证据；EnvironmentAcceptance 校验原始功能/清理产物的摘要、生产者、节点覆盖，以及 canary 资源与清理资源严格相等，12项验收绑定用例通过。

这些集合存在重叠，不累加。未将环境图、远端 grant 握手、全产品配置/鉴权安装、live harness、全 scope 生命周期等未完成接线标为环境阻塞或已完成。

## 环境图与逐步远端执行接线

以下为后续已实现增量，仍不代表整项交付完成：

- `EnvironmentScopePlan` / `EnvironmentRunService` 独立于 Case；冻结声明、所选节点、授权、runtime 摘要和批准的 repair plan。真实 prepare 必须使用 PostgreSQL。
- `environment:1` 已有 E1/E2/C1/C2/C3/T8/GE 实际 handlers。C2 记录原 intent，C3 新连接复检并限制 repair revisions/总步骤；所有分支经过 T8/GE。原 operation 不确定时只对账，查询允许保留到授权撤销后，新写入仍重验授权。共享宿主被占用时登记持久 wait，资源释放能唤醒独立环境计划，无需测试 Case。
- `RecipeRepairPlanner` 把一个产品 recipe 展开成逐原语 DAG，每个步骤进入现有 `OperationService`。`RemoteRecipeRunner` 走受信 SSH；helper 持宿主锁发送随机 nonce，在同一会话收到 grant 后才执行固定本地目录中的步骤。grant 持久化，资源 generation 跨 scope 单调，截止不超过线程租约。子进程继承宿主锁。
- `env check/plan/apply/worker/status/reconcile/report` 已有命令入口；只读检查输出原始证据和 JSON/Markdown/XLSX，计划绑定本地报告摘要与有效期；worker 拒绝 runtime 漂移，并只领取环境任务。报告、来源/安装检查并不能代替完整配置/注册/鉴权/canary 证明；当前默认 remote probe 对这些缺口明确 blocked。
- 修复 Kuasar 安装未复制 conductor/proxy/集群配置即调用 parser 的顺序问题；AgentENV 安装在 setup 前复制配置、systemd 环境文件及持久 shared key，控制角色配置与 key mount 也纳入步骤。配置和 binary 都要求显式 digest 资产。
- 环境与既有 Worker 组合 **194 passed**，证据 `environment-wiring.xml`；后续新增真实 LangGraph Driver/Worker 环境交付、资源释放唤醒、关闭 dispatch gate 后原操作对账、PID 复用反例等定向测试通过。194 不是这些后续改动的完整回归结果，最终还需重跑。

当前继续实施的代码缺口仍包括：全部产品配置/注册/鉴权与 canary 的默认装配，远端精确取消/文件补偿，T0 acceptance 接线、真实验收 harness，以及候选流程剩余主生命周期状态、review worker、标准门禁、bundle/晋级后验证和非 work scope 看护。它们不是“缺环境所以不用实现”。


## 当前本地实现进度（替代上文同名“仍需接线”的历史描述）

- 实际 environment:1 E1/E2/C1/C2/C3/T8/GE、命令恢复入口、nonce/grant/generation、原 PID/进程组取消及原文件 CAS 补偿已接入。C2 确认失败进入 C3 有限复检；unknown 保留原请求，不重复安装。报告来源摘要必须匹配声明；授权与报告保鲜期分别控制执行/准入。
- 实际 EvaluationDeployment 与 case 文件装配已交付；T0 环境验收校验原始功能/清理证明并支持所选节点子集；analysis/report 重跑的子报告采用本次期限。live 全链路和三产品×单机/多机 harness 已有代码，尚未执行真实环境验收。
- CandidateWorker、独立 reviewer、EvolutionCoordinator、候选取消、主 lifecycle 晋级事件、独立 worker bundle/领取证明已交付；本地冻结目录采用 staging→原子发布，重入核对原始树/修订身份。模型无进展/非法结果不写工作区。
- lifecycle status/request-repair/request-change/decision/cancel 与 gates run/show 已有 CLI；控制授权需精确作用域及当前 revision，取消复用 StopJobs 并仅针对当前 lifecycle。
- 固定 development/persistent/isolation/release 门禁文件与刷新脚本、真实隔离只读/网络/私有文件/原进程清理 canary、PostgreSQL lineage 最后一份预算竞争测试已增加。local_only 结果不能替代可信候选晋级门禁。
- 最新定向回归：环境212项；候选授权/worker/review/promotion/dispatch33项；委派+环境live合同49项；候选冻结重入/多change文档检查/grant/环境CLI41项。集合重叠，不累加；后续改动仍需最终完整回归。

仍在实施的本地缺口：普通CLI的可选模型/候选服务装配、产品配置/鉴权修复与纯管理节点证明、完整资源inventory及真实验收故障矩阵、G6发布后观察/冻结目标和新run部署绑定。不能把这些称为环境阻塞。外部验收条件为专用PostgreSQL、可用的Linux隔离/KVM/SSH目标及明确配置的模型bridge。
