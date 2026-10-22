# 环境适配输入与角色示例

环境声明是版本化输入。当前实现以 JSON/YAML 作为数据读取，不执行 YAML tag、shell 或模型生成命令；拓扑例子见 `docs/examples/environment/`。样例中的 `.example.invalid` 地址不可连接，适合只读 schema/profile 校验。它们没有安装制品摘要，**不能用于 apply 安装或调参**。

## 现状：环境检查（E1/E2）与环境配置（C1–C3）到底做了什么

> 本节只描述**已实现且有受控测试**的能力与**未实现边界**，不代表真实三产品节点可被自动检查或配置。进度以 [tasks.md](../openspec/changes/implement-sandbox-environment-adapters/tasks.md) 勾选为准（当前 **22/65**）。

### 图编排职责（`sandbox_graph/services/environment.py` 的 `EnvironmentHandlers`）

| 节点 | 职责 | 关键判据 |
|---|---|---|
| **E1 connect** | 只读连接/认证/身份核验，产出 `EnvironmentSnapshot{connected, authenticated, identity_confirmed, state}` | 任一为假 → `blocked` + `continuation='GB'` |
| **E2 inspect** | 按角色核验组件与**有效配置**，采集对象/依赖/在位证据 | `check`→`T8`；`satisfied`→`T0`；`drift`→`C1`；否则 `GB` |
| **C1 plan_changes** | 由 E2 的 drift 快照 + 冻结 plan 的 `conflict_domains` 形成配置计划，并原子领取冲突域租约 | 缺已验证适配（`verified/script/operations`）或 `check-only` → `blocked` |
| **C2 apply** | 经 `OperationService` 提交**注册脚本**操作（`script_ref`，不执行任意命令）；重入用同 `operation_id` 对账 | 非 `terminal+authoritative` → `blocked`/`GB` |
| **C3 verify** | **独立复检**（再次 `probe.inspect`）；通过则释放域，否则 `quarantine` | 复检通过才 `→T0` |

**红线**：测量运行中 **E1/E2 必经**（组图 `G0→E1→E2` 无条件）；**C1–C3 仅在 drift 时触发**；`check-only` 不允许配置；`C2` 不执行未注册命令。

### 已实现的产品无关公共底层（受控协议测试）

| 能力 | 模块 |
|---|---|
| 严格合同（声明/观察/修复计划/receipt/验收） | `contracts.py` |
| 引用摘要、依赖 DAG、角色适用性、可探测缺口合并 | `declarations.py` |
| 三产品 profile/version/arch + 制品 **digest 锁定** | `catalog.py` |
| **SSH transport**（OpenSSH argv、严格 known_hosts、限时限输出、安全 quoting） | `transport.py` |
| 访问探测（网络/身份/认证/执行权限/工具事实） | `probes/access.py` |
| **宿主探测**（CPU/内存/架构/内核/KVM/存储/网络/systemd/cgroup/观测权限） | `probes/host.py` + `scripts/environment/probe_host.py` |
| **鉴权探测**（无 key/错 key/正确 key 的正反例，区分健康与真授权） | `probes/auth.py` |
| **Linux 进程 inventory**（角色/组件、PID+start ticks、PSS/CPU、cgroup） | `probes/inventory.py` + `collect_inventory.py` |
| 配置解析 + 覆盖来源跟踪（file<env<CLI；`file_only` vs `runtime_effective`） | `config.py` |
| **原子文件事务**（预期摘要、symlink/归属、权限保持、CAS 补偿、受限备份） | `file_transaction.py` |
| 确定性修复计划（故障决策表 + DAG） | `planner.py` |
| 参数规则（`preserve/exact/relax_for_plan`、单位、有限预算） | `parameters.py` |
| 远端 journal（锁、原子持久、完整输入身份、进程/boot 记录） | `journal.py`、`remote_agent.py` |
| 环境执行网关（提交/查询/对账/逐动作 receipt） | `execution.py` |
| ExecutionGrant/nonce 握手（PG issuer） | `permits.py`、`grant_service.py` |
| 秘密存储（`secret://`、scope/version）、AuthBinding lifecycle | `secrets.py`、`auth.py` |
| 持久化前隔离/脱敏（哨兵秘密不泄漏） | `evidence.py` |
| 注册动作边界 | `scripts/authorized_key.py`、`service_action.py`、`product_install.py` |

### 尚未实现（当前真实边界）

- **三产品适配器**：`products/cubesandbox.py`、`products/kuasar.py`、`products/agentenv.py` 及真实发现/安装/调参/凭据/服务验收（tasks 组 5/6/7 **整组未完成**）。
- **环境 CLI**：`env check/plan/apply/worker/status/reconcile/report`（tasks 8.5/8.6）。
- **E1–C3 真实 LangGraph 接线**与恢复对账（tasks 8.3/8.4）。
- 宿主/共享服务冲突域 + PG issuer（4.5）、cancel/清理（4.7）、`verification.py`（4.8）。
- 三产品 **live harness**（10.2/10.3/10.4）与真实 Linux/PostgreSQL 验收。

**结论**：检查（连接→按角色核验）与配置（成计划锁域→注册脚本→独立复检）在**设计与通用底层已就绪并有受控测试**；但**三产品真实适配器、环境 CLI、真实图接线与真机验收尚未交付**，当前**不可对真实节点自动安装或声称环境 ready**。

环境组需显式列出 machine identity、角色、地址、SSH 用户、可信 known_hosts artifact ref 和自动化 credential secret ref。服务端/API/租户/数据面凭据分别使用 secret ref；密钥值不得写入 YAML、命令行或日志。`targets` 分开描述宿主、平台限制、单沙箱规格和性能策略。缺失且可探测的字段以带来源的探测事实补齐；声明值优先于探测值；未知的信任身份或凭据必须由管理者提供。

以 `cube-multinode-check.yaml` 为例，compute 节点只声明 compute 角色，并把既有 control API 作为外部管理端点。不会根据“单节点”字样推断一套 control+compute 安装。Kuasar 的单机例子显式将各个本地角色共置；AgentENV 的集群例子分别声明 scheduler/gateway 和 server 节点。实际主机地址、角色组合和产品版本由环境所有者替换。

进入任何变更计划前，必须锁定对应 profile/version/架构下所有安装制品的不可变 digest，填入 `artifacts`；固定源码 commit 只用于解释适配规则，不能代替 binary/package/container digest。Cube one-click 安装、AgentENV 默认 latest 下载入口都不能未经拆分直接使用。Kuasar 多组件包需要锁住兼容的聚合 release 和组件。当前仅有通用底层能力，三产品检查/安装/配置适配、环境图接线和正式 CLI 仍在实施；示例表达输入合同，不代表真实节点可通过 CLI 完成检查或变更。阶段状态、已验证范围和任务进度见本页末尾。

产品 API key 不采用统一 header 假设。审计版本的控制面主要使用 `X-API-Key`：Cube 还支持优先级更高的 callback/Bearer 配置；Kuasar 要把 APISecret/ManifestKey 绑定到租户；AgentENV 的 gateway 与 server 需使用一致的 key 版本。环境准备应分别验证服务端模式、正确 key 可访问和缺失/错误 key 被拒。公开健康检查不证明鉴权有效。实例命令/流量 token 绑定沙箱身份，不能替代管理 API key。

| 产品 | 服务端凭据来源与优先级 | 客户端请求及证明 | 重入与轮换 |
| --- | --- | --- | --- |
| CubeSandbox | 已配置 `AUTH_CALLBACK_URL` 优先于 `CUBE_API_KEY`；没有任一项时受保护路由可能匿名放行 | 统一基线发送 `X-API-Key`；Bearer 仍按产品优先级处理。限流 middleware 按 `X-API-Key` 分桶，Bearer-only 请求可能进入 anonymous 桶，比较数据须标明 carrier | callback 由既有身份系统管理；静态 key 服务端环境与测试 client 同版本，变更后重启并正反例复检；不因 apply 重置 key |
| Kuasar | 本机管理 UDS/`node-ctl manifest-key` 管理 `ManifestKey` 与 `APISecret`；普通 API 外层仅作格式解析，业务层还验证 MAC 和资源归属 | 普通 API 可用 Bearer 兼容模式，统一测试基线用 `X-API-KEY`；exec-session 仅认此 header。200 空列表/格式正确不证明密钥属于正确租户 | 先 check 再 add 防止重复登记刷新 TTL；多机集群经 `placer import_groups` 的受控 file source 配置 group JSON 并核验下发；旧版本保存到私有 store |
| AgentENV | server 使用 `AENV_API_KEY`，随后 `/run/secrets/api-key`，节点再依产品规则回退 managed key；gateway 使用环境变量或外部 secret 文件，不自行生成。空环境变量按错误处理 | server/gateway 请求都用 `X-API-Key`；直接访问与经 gateway 路径分别检查，成功证据须匹配该环境组和 key version | 新集群先统一生成并持久 key，再配置 gateway 与全部 server；轮换须冻结单 key 中断窗口，不能只更新 benchmark client |

权限检查每次都发三种有界请求：无 key、符合产品格式但错误的 key、正确 key。拒绝状态码依据具体 API 路由声明；正确访问可验证资源归属。健康和 metrics 端点不用于鉴权证明。API key 与 SSH 登录私钥、Kuasar 组 root key、存储加密 key、`envdAccessToken` 及流量 token 各有独立 scope 和生命周期。

建议运行顺序是：只读 check → 审阅差异和固定制品 → 冻结 plan → 对授权范围 apply → reconcile 未决步骤 → 独立复检 → 归档脱敏报告。当前仅 check 行为及资料校验可使用未来 CLI 合同；计划、apply 和 worker 命令尚未交付。执行器需要外部 secret/artifact 存储配置和持久运行数据库；检查环境不满足时应产出 blocker，而不是猜测新装。

SSH 只用固定 `sandbox-graph-remote-agent --stdio-json` helper 请求，不在远端拼接环境声明中的 shell 文本。`known_hosts` 必须由环境所有者提供且严格验证；私钥文件要求当前用户独占访问权限。主机连通、host key 可信、SSH 登录、机器身份一致和远端执行权限是独立检查结果。设置自动化公钥需要有效的初始凭据；已存在条目按 key blob 幂等复用，验证另用新 SSH 连接完成。

`scripts/environment/probe_host.py --role compute` 在 Linux 本机只读收集事实并输出 JSON。KVM 的设备不存在、模块未加载、CPU 不支持虚拟化和设备权限不足含义不同；只有权限类差异有软件侧修复路径，不能把物理硬件缺失标成可自动修复。纯管理角色的 KVM 检查为 `not_applicable`。HarmonyOS/macOS 开发机可运行合同与规划测试，不能替代该 Linux 主机探测验收。

环境配置输入严格解析并保留来源顺序：文件值 < 环境变量覆盖 < 命令行覆盖。远端当前值如果只能从文件或解析器观察，报告应标成 `file_only` / `resolved_only`；必须有进程/服务的独立运行时证据才可标记 `runtime_effective`。结构化编辑先比对冻结前摘要；冲突时保留现场文件并产生新计划。受限备份可能包含密钥，必须留在私有存储区，报告仅记录私有 artifact ref 与摘要。

### Linux process inventory

`python scripts/environment/collect_inventory.py --rules docs/examples/environment/inventory-rules.example.json` emits a versioned JSON inventory. Rules match executable basenames to one role/component; duplicate matches are reported as ambiguous and excluded from component totals. Each process identity combines PID and `/proc/<pid>/stat` start ticks to detect PID reuse. PSS comes from `smaps_rollup`; CPU usage is reported as process ticks with the host tick frequency, and cgroup/container identity stays attached to each observation. Permission gaps, process-exit races, missing PSS and ambiguous ownership are preserved and return a nonzero coverage status. The fixture is illustrative only; product-specific rule sets must be source-audited before reporting component totals.


## 当前实现与未完成范围

OpenSpec change：[`implement-sandbox-environment-adapters`](../openspec/changes/implement-sandbox-environment-adapters/)。当前任务清单为 **22/65 完成**；这个数字表示逐项实现任务状态，不代表环境能力完成比例。

已实现并有受控测试的公共底层包括严格声明/报告/动作合同，JSON/YAML 输入校验与来源 catalog，SSH transport、访问及 Linux 主机探测，配置解析/原子文件事务，私有 secret store 和鉴权 lifecycle/探针，evidence 脱敏，确定性修复计划与有界参数规则，远端 journal、环境 OperationService gateway、注册式服务/安装动作，以及通用 Linux 进程 inventory。完整范围和单项状态以 [tasks.md](../openspec/changes/implement-sandbox-environment-adapters/tasks.md) 勾选为准。

**尚未实现完整的环境检查和配置能力**：`products/cubesandbox.py`、`products/kuasar.py`、`products/agentenv.py` 产品适配器和对应真实发现/修复规则尚未交付；环境专用 check/plan/apply/reconcile CLI 与 LangGraph E1/E2/C1/C2/C3 接线尚未交付；参数生效复检、三产品安装配置及各自 live harness 也未完成。当前不会自动完成真实三产品节点的检查、安装、调参或验收。

最新本地验证：`.venv/bin/python -m pytest -q` 为 **521 passed、4 skipped、101 subtests passed**；`openspec validate implement-sandbox-environment-adapters --strict` 通过。测试通过只证明已实现代码的自动化检查，不代表 Linux/PostgreSQL 或真实沙箱部署验收通过。
