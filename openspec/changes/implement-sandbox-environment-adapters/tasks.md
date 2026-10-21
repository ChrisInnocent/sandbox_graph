# Tasks

范围及验收分别见 [spec](specs/sandbox-environment-adapters/spec.md) 和 [design](design.md)。下列均为待实施任务；规划文件存在不代表代码实现或真实安装成功。EAT 编号对应 design D14 的测试合同。每项新增行为先补失败测试再实现；缺真实目标只影响明确标注的 live 验收，不免除代码交付。

依赖：1→2/3→4；5/6/7 复用 1—4 的合同与执行基础，8 将三产品接入环境图，9 接入测试及报告，10 做跨层验收。任务组内测试、文档随实现交付，不推迟到最后统一补写。D08 鉴权是各产品必交部分，不仅实现测试机的 SSH/环境变量。

## 1. 合同、来源与环境声明

- [x] 1.1 新增 `sandbox_graph/environment/contracts.py`，定义 D03 的声明、观察、修复计划、步骤 receipt 和验收合同，严格版本/枚举/引用/范围校验；以 EAT-01 检验单节点兼任、外部管理、重复身份和明文 secret 拒绝。
- [x] 1.2 实现 `declarations.py` 的引用摘要验证、依赖 DAG、角色适用性及可探测缺口合并；EAT-01 覆盖缺字段、环、错误 scope 与事实来源，不能虚构环境输入。
- [x] 1.3 实现 `catalog.py` 的三产品 profile/version/arch 和 source/artifact lock；EAT-02 覆盖未知版本、浮动下载、摘要不符，确认不可修改环境。
- [x] 1.4 在 `tests/environment/fixtures/` 固化带源码位置的脱敏配置、角色拓扑和独立 golden vectors；EAT-02 验证 fixture 来源和适用版本，不能用被测函数生成期望值。
- [x] 1.5 编写 `docs/environment-adapters.md` 的输入合同和三产品单机/多机示例，含角色、制品、auth refs、规格和实验预算；所有示例通过 EAT-01，秘密不进入 Git。

## 2. 真实访问、宿主探测和配置事务

- [x] 2.1 实现 `transport.py` 的 OpenSSH argv、严格 known_hosts、限时/限输出、结构化 stdin 与安全远端 quoting；EAT-03 覆盖认证失败、host key 变化、超时及命令注入输入。
- [x] 2.2 实现 `probes/access.py`，分别产出网络/身份/认证/执行权限和工具事实；EAT-03 断言访问失败后无安装、公钥或配置写调用。
- [x] 2.3 实现 `scripts/environment/authorized_key.py` 和访问 adapter：复用或 CAS 添加公钥，保护既有条目并独立新连接验证；EAT-04 覆盖重复运行和写后认证失败。
- [x] 2.4 实现 `probes/host.py` 与 `scripts/environment/probe_host.py`，读取 CPU/内存/架构/内核/KVM 可用性、存储、网络、systemd/cgroup 和观测权限；EAT-05 覆盖角色不适用、硬件缺失与权限不足。
- [x] 2.5 实现结构化 YAML/JSON/TOML 配置解析与覆盖来源跟踪，按需锁定编辑依赖；EAT-06 验证 target/file/resolved/effective 不混淆、未知字段保留和非法配置拒绝。
- [x] 2.6 实现 `scripts/environment/file_transaction.py`：预期摘要、symlink/归属检查、权限保持、原子替换、受限备份及 CAS 补偿；EAT-07 验证崩溃和外部并发修改不会留下半文件或覆盖他人变化。
- [x] 2.7 在环境文档说明访问阶段只读边界、初始/自动化凭据转换和最小采集权限；用 EAT-03—07 的真实协议 fixture 验证文档操作与实现一致。

## 3. 秘密存储及双端鉴权公共合同

- [x] 3.1 实现 `secrets.py` 的受限文件后端、`secret://` 引用、scope/version 校验与多 worker 可解析性约束；EAT-28 检查目录/文件权限、原子唯一生成与生成后崩溃重入。
- [x] 3.2 实现 `auth.py` 的 AuthBinding 与 reuse/create_if_absent/rotate 状态机，分别处理 SSH、控制 API、租户及实例 token；EAT-28 验证未决操作不重生成、不误撤销既有凭据。
- [x] 3.3 实现凭据保密通道及 `evidence.py` 的持久化前隔离/脱敏，覆盖 stdout/stderr、HTTP、环境、argv、错误栈与报告；EAT-21/EAT-28 使用哨兵秘密确认公开产物无泄漏。
- [x] 3.4 实现 protected endpoint 的有界正反例执行器和语义化 verdict，区分健康、格式检查、真正授权与业务错误；EAT-25—27 验证 health/空列表/429 不能作为统一鉴权成功或失败依据。
- [x] 3.5 编写鉴权矩阵及服务端/客户端绑定运维说明，覆盖三产品 carrier、优先级、轮换中断和外部 callback 保留；EAT-25—28 fixture 核对矩阵，不出现统一 `x-api-token` 假设。

## 4. 修复计划、逐步执行和恢复

- [x] 4.1 实现 `planner.py` 的 D09 故障决策表、动作目录及依赖 DAG；EAT-08 验证确认缺失/停止/unknown 的不同路径和固定输入产生稳定计划摘要。
- [x] 4.2 实现 `parameters.py` 的 preserve/exact/relax_for_plan、单位和有限预算计算，区分平台限制、单实例规格与性能策略；EAT-15 验证不使用任意最大整数或隐式关闭资源控制。
- [x] 4.3 实现 `remote_agent.py` 与远端 journal 的锁、原子持久、完整输入身份和进程/boot 记录；EAT-16 在 accepted/started/terminal 边界注入断连，验证同 ID 不同输入拒绝。
- [x] 4.4 实现 `execution.py` 的 EnvironmentRunnerGateway，接入 OperationService，支持提交/查询/对账及逐动作 receipt；EAT-16 验证 unknown 不重提、已完成复用及 journal 缺失时隔离。
- [ ] 4.5 实现宿主/共享服务冲突域及 D10 的 ExecutionGrant/nonce 握手、PG issuer、版本化 gateway、独立资源 generation、时钟偏差边界和远端持锁；EAT-16/EAT-17 验证延迟过期请求、nonce 重放、PG 失联、旧子进程持锁及不同 thread epoch 均不能越权开始步骤。
- [x] 4.6 实现 `scripts/environment/service_action.py` 与 `product_install.py` 的注册动作边界、超时和可核验服务执行；EAT-16/EAT-19 证明退出零不等于验收通过、任意模型命令不能执行。
- [ ] 4.7 实现 cancel、可归属清理、文件补偿和准备 canary 的独立动作合同；EAT-18 覆盖 ACK 后仍运行、未知创建、他人资源和清理失败不释放隔离。
- [ ] 4.8 实现 `verification.py` 的新连接复检和有限 repair revisions/attempts；EAT-19 验证相同差异到限停止，不把未决步骤作为普通失败重试。
- [ ] 4.9 在环境文档记录中断后的 status/reconcile、可重试条件和回滚边界；以 EAT-16—19 的故障序列检查每个恢复步骤都能定位原操作与证据。

## 5. CubeSandbox 产品落地

- [ ] 5.1 实现 `products/cubesandbox.py` 的单机/control+compute/compute-only 部署发现、路径与 binary/config/service 身份检查；EAT-09 验证纯管理节点不能被 one-click control 静默扩为计算节点。
- [ ] 5.2 实现固定 one-click 制品和依赖的分阶段安装动作，新装/显式升级/部分已装分别处理；EAT-09 核验真实 command/config 载荷及已有配置不被重复 install 重置。
- [ ] 5.3 实现计算节点注册地址、依赖服务、运行网络和管理侧上报验收；EAT-09 以缺注册/陈旧状态/部分服务失败验证组就绪判据。
- [ ] 5.4 实现 Cubelet/CubeMaster/API 的参数解析、修复与生效验证；EAT-10/EAT-15 覆盖上报与管理 max 合并、零值回退、reserve 比例、弃用字段和 0→1 API rate。
- [ ] 5.5 实现 Cube 服务端静态 key 配置、真实启动覆盖识别和 callback 模式保留，再绑定客户端；EAT-25 验证正确/缺失/错误凭据、callback 优先及 Bearer/X-API-Key 限流差异。
- [ ] 5.6 编写 Cube 单机/多机 live 验收 harness 与步骤证据断言，并更新对应 profile 文档；EAT-31 可显式选择真实环境运行，默认缺环境清晰 skip，不能合成安装成功。

## 6. Kuasar 产品落地

- [ ] 6.1 实现 `products/kuasar.py` 的聚合版本、conductor/proxy/runtime、运行根和可选 store/cache 发现；EAT-11 验证不匹配制品及缺 proxy 不 ready。
- [ ] 6.2 实现锁定源资产、目录/网络、node-ctl/sandbox-ctl 与 systemd units 的原生安装动作；EAT-11 核验实际命令、配置路径与来源依赖，不重建既有存储 generation。
- [ ] 6.3 实现 registry/router/placer 等声明角色配置、node-link/proxy 注册和有界双向握手；EAT-11 覆盖多机监听先启动、注册后验收，避免双方等待造成死循环。
- [ ] 6.4 实现 capacity、resource_listen、builder、pool 及实例 capacity 参数规则与官方解析验证；EAT-12/EAT-15 覆盖 0→4/16/256、builder 非法零及 guest/host 容量隔离。
- [ ] 6.5 实现单机租户 APISecret/ManifestKey 的 check-before-add 与 e2b-key-ctl 签发/客户端绑定；集群实现受控 group JSON 目录和 placer `import_groups` 文件源的原子配置、导入及下发验证；EAT-27/EAT-28 覆盖既有 TTL 保持、重复 group/source、密钥对 inline/ref/派生格式与导入失败，不使用不存在的 group 管理命令。
- [ ] 6.6 实现 MAC/资源归属的鉴权验收、exec-session carrier 与只检查不足证明的分级；EAT-27 验证格式合法但 MAC 错误、未登记、错租户、空列表 200 不能通过。
- [ ] 6.7 编写 Kuasar 单机/多机 live harness、源资产和租户配置文档；EAT-32 具备实际独立复检、重入与清理断言，未提供目标必须清晰 skip。

## 7. AgentENV 产品落地

- [ ] 7.1 实现 `products/agentenv.py` 的 native server、scheduler/gateway、home/runtime/config 和实际进程发现；EAT-13 验证角色适用性与已有定制部署不被误安装。
- [ ] 7.2 实现固定 server/CLI/ublk/runtime 资产、专用账户/目录权限、setup-only/setup-host 与 systemd 安装步骤；EAT-13 验证不执行浮动 latest、在用模块不强卸载及关键 unit 属性。
- [ ] 7.3 实现控制角色固定镜像/Compose 配置、跨主机静态节点端点与上报；EAT-13 覆盖示例 Docker 名称不能误用于跨主机、缺报告不 ready。
- [ ] 7.4 实现 node_resource_limit 的 nil/0/paused 语义、构建并发、ublk 当前/目标值及实际规格校验；EAT-14/EAT-15 验证等于阈值的 `>` 边界和 2v4G 不被调参改变。
- [ ] 7.5 实现 AENV_API_KEY/外部文件/managed key 的发现、复用和服务端部署，gateway 与节点绑定同一版本；EAT-26/EAT-28 验证空 env 非回退、格式约束、各节点不重复生成和恢复不重置 key。
- [ ] 7.6 实现 gateway 与节点直连的 protected route 正反例及必要代理入口验证；EAT-26 验证 health 不当鉴权证明，旧客户端或错误凭据不能静默匿名回退。
- [ ] 7.7 编写 AgentENV 单机/多机 live harness 与 profile 文档，包含 ublk、专用账户、shared key 和报告；EAT-33 缺环境清晰 skip，真实运行保留执行与清理证据。

## 8. 环境图、命令入口和恢复接线

- [ ] 8.1 实现 EnvironmentScopePlan/EnvironmentRunService 和环境计划持久化，范围来自环境声明不依赖 Case；EAT-22 验证零测试能力也能规划检查与准备。
- [ ] 8.2 注册不可变 `environment:1` 定义与真实 adapter/runner handlers，保持旧 graph_ref 的定义和工厂；EAT-22 验证实际选择不同 handler，旧运行可恢复。
- [ ] 8.3 接入 E1/E2/C1/C2/C3、有限 revision 与 T8/GE 环境交付分支；EAT-19/EAT-22 验证 check-only 无副作用且失败报告不会成为环境 ready。
- [ ] 8.4 接入 group 作业恢复、原操作对账和环境资源等待/释放唤醒；EAT-17/EAT-23 验证 C2 已完成的重启运行进入 C3，未知不重复安装或生成 key。
- [ ] 8.5 实现 `env check/plan` CLI、退出状态和本地 artifact 导入校验；EAT-35 验证只读远端、过期报告拒绝及无 Case 场景可用。
- [ ] 8.6 实现 `env apply/worker/status/reconcile/report` CLI 和持久依赖装配；EAT-35 验证缺 PostgreSQL 拒绝真实修改、request-id 幂等、客户端退出不丢失操作。
- [ ] 8.7 更新 README 的真实环境入口和 `docs/maintenance.md` 的新图/adapter 增删改及恢复方法；EAT-35 执行所列 help/fixture 命令，示例不能指向未实现入口。

## 9. 资源证据、测试绑定和归档

- [ ] 9.1 实现 `probes/inventory.py` 与 `collect_inventory.py` 的角色/组件、PID/start-time、cgroup/container、共享对象及观测权限映射；EAT-20 验证 PID 复用、重复归属和 PSS 缺口。
- [ ] 9.2 实现 `reporting.py` 的完整 JSON manifest 及 Markdown/Excel 摘要，保留失败节点、安装步骤和既有环境的历史不可得状态；EAT-21 校验引用完整性和秘密扫描。
- [ ] 9.3 实现 `bindings.py` 和新版 T0 对 EnvironmentAcceptance 的版本/配置/身份/有效期/cleanup 校验；EAT-24 验证漂移仅使相应范围失效且不解除方法 capability gate。
- [ ] 9.4 接入 benchmark 的 `auth_ref` 和兼容 `api_key_env`，凭据只由执行器临时注入，服务端版本与客户端一致；EAT-24/EAT-28 验证旧 key、错 scope 和缺 secret 均阻塞。
- [ ] 9.5 接入真实实例数据面 token 的私有存储、generation 绑定与恢复；EAT-29 验证不同实例隔离、重建失效和 STATE_READY 不消费 FIRST。
- [ ] 9.6 更新父 change 的环境相关设计/规格说明与任务交叉引用，明确新旧合同及脚本/模型/人工边界；扩展 `scripts/verify_planning.py` 的多 change 发现，EAT-34 检查链接/编号及不依赖固定旧任务总数。
- [ ] 9.7 在环境使用文档说明检查报告、安装证据、鉴权策略对比较的影响及动态 inventory 刷新；用 EAT-20/EAT-21/EAT-24 产物核对示例，不能展示伪测量值。

## 10. 跨层集成与真实环境验收

- [ ] 10.1 在具备条件的 Linux 上运行真实 SSH/systemd/proc/KVM 原语集成（EAT-30）和 PostgreSQL fencing/图恢复测试（EAT-17/EAT-22/EAT-23），记录实际环境及 skipped 原因；受控通过不能替代此项通过。
- [ ] 10.2 运行 Cube 单机及多机真实验收 EAT-31，覆盖新装/复用/部分失败恢复、调参、服务端鉴权正反例、canary 清理和完整报告；无目标则保持本项未完成并记录缺字段。
- [ ] 10.3 运行 Kuasar 单机及多机真实验收 EAT-32，增加 proxy/租户登记/MAC/源资产证据；无目标保持未完成，不用协议 fixture 冒充真实运行。
- [ ] 10.4 运行 AgentENV 单机及多机真实验收 EAT-33，增加 gateway/shared key/ublk/节点报告证据；无目标保持未完成，不用代码存在代替安装成功。
- [ ] 10.5 运行完整 environment、现有 orchestration 与 benchmark 回归及 OpenSpec strict 校验；保存 pass/fail/skip 分层结果，核对三产品均有实际执行路径、未完成 live 项仍未勾选。

## Requirement Traceability

| 需求 | 主要任务 | 验收合同 |
| --- | --- | --- |
| EAD-01 | 1.1—1.2、8.1 | EAT-01、EAT-22 |
| EAD-02 | 1.3—1.4、5.2、6.2、7.2 | EAT-02、EAT-09、EAT-11、EAT-13 |
| EAD-03 | 2.1—2.2 | EAT-03 |
| EAD-04 | 2.4、4.1、8.3 | EAT-05、EAT-08、EAT-22 |
| EAD-05 | 2.4、5.1、6.1、7.1 | EAT-05、EAT-09、EAT-11、EAT-13 |
| EAD-06 | 2.5—2.6、5.4、6.4、7.4 | EAT-06、EAT-07、EAT-10、EAT-12、EAT-14 |
| EAD-07 | 4.1—4.2 | EAT-08、EAT-15 |
| EAD-08 | 2.3 | EAT-04 |
| EAD-09 | 5.1—5.3、5.6、10.2 | EAT-09、EAT-31 |
| EAD-10 | 5.4 | EAT-10 |
| EAD-11 | 6.1—6.3、6.7、10.3 | EAT-11、EAT-32 |
| EAD-12 | 6.4 | EAT-12 |
| EAD-13 | 7.1—7.3、7.7、10.4 | EAT-13、EAT-33 |
| EAD-14 | 7.4 | EAT-14 |
| EAD-15 | 4.2、5.4、6.4、7.4 | EAT-15 |
| EAD-16 | 4.3—4.4、8.4 | EAT-16、EAT-23 |
| EAD-17 | 4.5、4.7、8.4 | EAT-17、EAT-18 |
| EAD-18 | 4.8、8.3 | EAT-19 |
| EAD-19 | 4.7、9.3、9.5 | EAT-18、EAT-24、EAT-29 |
| EAD-20 | 9.1 | EAT-20 |
| EAD-21 | 3.3、9.2 | EAT-21 |
| EAD-22 | 8.1—8.6、9.3 | EAT-22、EAT-23、EAT-24、EAT-35 |
| EAD-23 | 4.1、4.6、9.6 | EAT-08、EAT-16、EAT-34 |
| EAD-24 | 5.6、6.7、7.7、10.1—10.5 | EAT-30、EAT-31、EAT-32、EAT-33 |
| EAD-25 | 3.4—3.5、5.5、6.6、7.5—7.6 | EAT-25、EAT-26、EAT-27 |
| EAD-26 | 3.1—3.2、5.5、6.5、7.5、9.4 | EAT-25、EAT-26、EAT-27、EAT-28 |
| EAD-27 | 3.2、3.4、5.5、6.6、7.6 | EAT-25、EAT-26、EAT-27、EAT-28 |
| EAD-28 | 3.3、9.4—9.5 | EAT-21、EAT-28、EAT-29 |

## Workflow follow-up

- 仅在实现及对应验证完成、剩余真实验收有明确处理后再执行 OpenSpec 同步/归档；规划完成不代表可归档。
- 提交或发布按照用户后续明确请求执行；公开归档只包含脱敏证据，不包含现场凭据或私有日志。
