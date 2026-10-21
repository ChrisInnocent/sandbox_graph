# 图编排代码测试设计

## Context

2026-10-21，测试设计草案，全部用例为 planned；没有新增可执行测试、运行 LangGraph 或连接真实沙箱。被测对象是 [orchestration-design.md](orchestration-design.md) 的编排系统，而 [test-nodes.md](test-nodes.md) 定义被测沙箱的性能实验，两者不能混作一套验收。

测试依据为 WF-01–WF-36、环境阶段的路由／作用域要求、运行时场景合同的传递与测量证据边界。本文覆盖调度、状态、恢复、交付和测试 runner 接口；产品内部性能算法和真实 PSS 采集精度需要后续脚本及适配测试验证。

## Goals / Non-Goals

**Goals:** 用可重复输入、可控故障与独立断言证明编排满足需求，发现“脚本退出成功但数据不合法”“checkpoint 恢复却重复创建”“收到所有事件却引用旧报告”等错误。

**Non-Goals:** 不用 mock 通过替代三套产品适配通过；不将本机调度耗时当沙箱性能；不通过模型主观判断测试是否通过；不设未经用户确认的真实性能阈值。

## Decisions

### T01. 分层策略与拟议测试目录

| 层 | 拟议目录 | 实际被测组件 | 替身边界与通过证据 |
|---|---|---|---|
| L0 纯逻辑 | `tests/orchestration/unit/` | schema、计划编译、路由、转换、复用和覆盖 | FakeClock、固定目录；精确集合／状态断言 |
| L1 图集成 | `tests/orchestration/graphs/` | 真实 StateGraph、wrapper、条件边、interrupt、thread 隔离 | fake 业务服务；每例新 saver，测试边序列与状态 |
| L2 持久集成 | `tests/orchestration/persistence/` | 真实 PostgreSQL、正式 saver、outbox、CAS、租约与文件发布 | fake 产品接口；实际事务和双 worker 并发 |
| L3 崩溃恢复 | `tests/orchestration/recovery/` | 独立 controller/runner 子进程、持久存储、重启恢复 | ControlledRunner 可制造响应丢失；必须硬杀进程，不能只抛可捕获异常 |
| L4 端到端编排 | `tests/orchestration/e2e/` | 五阶段、三级责任、增量分析、部分归档 | 合成样本与产物，不使用真实候选名称冒充测量 |
| L5 真实适配验收 | 后续 adapter 测试计划 | 真实候选及远端部署 | 需真实环境合同；本轮只保留交接条件，不声称覆盖 |

拟议工具为 pytest 与异步测试支持，属性测试可用 Hypothesis。实现时锁版本；每例新建 graph/thread/数据库 namespace，避免隐式共享历史。官方说明直接调用单个 node 会绕过 checkpointer，所以它不能证明持久恢复正确。[官方测试指南](https://docs.langchain.com/oss/python/langgraph/test)

L2/L3 必须使用 D01 选定的正式持久后端；InMemorySaver 的成功不能满足重启验收。真实数据库不可用时明确 SKIPPED/BLOCKED，并且不能把持久验收写为 passed。

### T02. 夹具、时钟与独立 oracle

全部夹具为**合成工程数据**，不代表用户已确定的真实环境或压测参数：

- `G_A`：一台 host_A，管理与数据角色共置；`G_B`：host_B_control 与 host_B_worker 的集群。第三组 `G_C` 与 G_A 共享 host_A，用于资源冲突测试。
- `catalog_v1`：B01/B04/B09/B10.FIXED_REQUESTS.WINDOW/BATCH 及一个未选择扩展；三个合成适配状态为 supported、unsupported、unverified。其余 B/E 方法用合同矩阵参数化验证，不各复制一套图。
- `case_alpha`：有限两种显式 condition tuple、两次 repeat；授权范围只含 G_A/G_B。未知组和不合法 tuple 用于负例。
- ControlledRunner：以 operation_id 持久存储启动次数、HTTP 接纳、真实完成、实际资源、取消结果。可以在指定边界停住，由测试 barrier 决定继续／崩溃，不能仅靠随机 sleep 制造竞态。
- FakeClock 区分 monotonic、wall、process_epoch；重启时 monotonic 不能与前进程直接相减。持久后端租约测试使用数据库时间及同步 barrier，验证 deadline 边界。
- ArtifactFixture：有效、缺文件、摘要不符、跨组引用、截断、敏感输出、可重算原始数据和不兼容合同数据。
- SpyModel：记录调用与输入；允许返回越权操作、未引用事实、伪造结果或超预算内容，验证它们不能影响确定性状态。
- 所有随机／属性测试保存 seed、生成事件序列、失败最小反例和依赖版本。

独立 oracle 不调用生产 reducer／compile 函数生成期望值：选测预期用显式集合，恢复预期读取 ControlledRunner 的真实副作用账本，事件预期由小型不可变参考模型计算。只检查“调用了某函数”不足以证明行为。

每例证据至少包含 test_id、fixture_hash、代码／图／库版本、输入、事件顺序、前后状态、断言、操作启动次数、产物 manifest；失败要保留进程日志、账本和资源快照。成功临时资源可清理，但测试摘要必须保留。

### T03. 必须保持的不变量

| ID | 不变量 | 独立观察方式 |
|---|---|---|
| I01 | 实际执行集合不超计划与授权；无匹配不变全量 | 比较计划集合与实际 operation ledger |
| I02 | 同一逻辑操作不因重放重复产生效果；新 repeat 不被去重吞掉 | runner 启动／副作用数，operation 与 work 身份 |
| I03 | 失效 owner 无法改当前状态；未知在途效果不会释放冲突域 | 两 worker 写入结果与 quarantine 查询 |
| I04 | 成功、有效性、验收、性能和清理相互独立 | 对分离状态逐项断言，保留旧事实 |
| I05 | 计数来自去重后的身份和事件，不来自消息次数 | 重复与乱序输入的参考集合 |
| I06 | 同一分析／报告引用精确输入版本 | manifest hash 与 closure revision |
| I07 | 任何失败／取消路径都有证据和清理记录 | 遍历可达失败边并核对 resources |
| I08 | 未定义条件不能被模型填成事实 | SpyModel 越权建议与前后状态差异 |
| I09 | 子节点上下文及产物访问不越 scope | 实际序列化上下文、拒绝读取结果 |
| I10 | 采样、发压、等待、重试均有边界；数据缺失不当零 | 时钟推进、调用数、缺失状态 |
| I11 | 已完成的局部工作不因其他组失败而全部重跑 | 每个 work 的实际执行次数 |
| I12 | 有效失败和不可比较项始终保留覆盖位置 | 计划分母、报告条目与状态 |
| I13 | 同一 WorkKey／比较键不含混端点；白盒 `guest_ready` 不与黑盒 `state_ready` 合并 | 分组查询比较键与实际样本 scope |
| I14 | 声明管理面入口但未核验到管理面角色即 blocked；未解析来源／端点不被默认值填充 | 拓扑核验调用记录与 case 字段、blocked 出口 |

### T04. 选测、图定义与环境路由

以下每行是一组可独立执行的测试合同，实施时按列出的变体参数化。所有测试 ID 在本文件内唯一。

| ID／层 | Given / 注入 | When | Then：精确预期 |
|---|---|---|---|
| ORC-001 L0 | 同轴 A/B、跨轴方法 M、排除项 X | 编译 selection | 结果为显式预期集合 `(A∪B)∩M−X`；不产生其他 tuple |
| ORC-002 L0/L4 | 未知 ID、显式空数组、合法零匹配分别输入 | preview/start | 分别为配置错误／空计划／无匹配；远端 submit=0 |
| ORC-003 L0 | 只选 B04，无暂停 fixture | 补依赖 | 只增加创建／暂停准备和验证；不增加 B01/B03 性能用例 |
| ORC-004 L0 | 必需暂停准备被显式排除 | compile | 给出受影响 B04 与冲突原因；不覆盖排除 |
| ORC-005 L0/L4 | 多候选中一个 unsupported、一个 unverified | 展开同方法 | 保留两个覆盖项；分别 TC/TB；无虚假耗时和零分 |
| ORC-006 L0 | 未选 E04/E06，基础用例出现失败 | 诊断建议扩测 | 仅产生建议或 RP 差异；扩展 submit=0 |
| ORC-007 L0/L2 | preview 后授权／关键环境 evidence 改变 | start 原 hash | 拒绝静默使用新集合；返回重验／新预览原因 |
| ORC-008 L0 | 删必要节点、未知 handler、类型不兼容、无界环 | validate graph patch | 拒绝并定位依赖；历史 graph 不变 |
| ORC-009 L0/L1 | 有界重试环、持久等待环 | 校验并编译 | 合法；不能把全部控制流当 DAG；预算耗尽可退出 |
| ORC-010 L1 | 同一节点兼有固定出边和动态 goto 的非法定义 | factory build | 静态拒绝；避免双分支执行 |
| ORC-011 L1/L4 | 环境已满足、有可修复差异、探测未知三变体 | E2 路由 | 分别 T0／C1／GB；未知不安装；在位路径 C2=0 |
| ORC-012 L1/L4 | only-check 且组件缺失 | 执行检查范围 | 产出差异报告与范围归档；C2=0，不转性能测量 |
| ORC-013 L1 | 单机角色共置；单机仅数据角色+外部管理依赖 | E1/E2 | 不制造多环境组；不因单机就安装管理组件 |
| ORC-014 L1 | 端口可达但认证失败；主机身份冲突 | E1 后路由 | 单列访问断言；公钥配置／安装 submit=0 |
| ORC-015 L1 | C1 有模型方案但无已验证适配；C2 exit=0 但服务未生效 | 分别执行配置流程 | 前者 blocked 不提交；后者 C3 拒绝放行，不进依赖测量 |

### T05. 状态、agent 与工作节点路由

| ID／层 | Given / 注入 | When | Then：精确预期 |
|---|---|---|---|
| ORC-016 L0/L1 | 脚本 exit=0，但 raw 必需事件缺失 | N08/T6 | execution=succeeded、data=insufficient、review=rejected；不填 pass |
| ORC-017 L1/L4 | 功能有效、延迟超过明确阈值 | 分析与收口 | data=valid、performance=fail；正常出结论，测量启动仅一次 |
| ORC-018 L0/L2 | 失败尝试后重试；独立新增 repeat | 执行身份生成 | 原失败保留；新 attempt 关联原因；新 repeat 真执行不被旧 receipt 吞掉 |
| ORC-019 L0/L2 | 相同 revision 重复／冲突；旧 revision 迟到 | 更新 keyed 状态 | 重复幂等，冲突报错，旧版本不覆盖当前；完成数不增加 |
| ORC-020 L1/L4 | 每个节点均成功且规则完备 | 五阶段确定性路径（报告解释用固定夹具） | 调度／采样／统计分支 SpyModel 调用=0；F1 模型策略单独验 |
| ORC-021 L1/L4 | 两组含不同合同和秘密引用 | 构造 N04/N05 及组上下文 | 每节点仅 own scope；不含其他组日志、原始秘密及全量对话 |
| ORC-022 L1 | 模型返回伪造指标、越权脚本或无引用“事实” | 验证 proposal | 拒绝转执行事实；未注册 submit=0；已有合法记录不变 |
| ORC-023 L0/L1 | 大日志与超限模型回复 | 构造上下文／读取证据 | 预算内分页或受限失败；不静默截掉关键失败；usage 缺失=unknown |
| ORC-024 L1/L4 | N02/N03/N04/N05/N06/N07/N08 逐点失败 | 运行工作图 | 禁止后续测量；保存已有证据；均尝试 N09 并交付缺口 N10 |
| ORC-025 L1 | 零实例基线方法，前置创建残留实例 | N03→N04 | 有权复位后才采空基线，否则 blocked；不得基线污染后放行 |
| ORC-026 L1 | STATE_READY 与 EXEC_COMPLETE 两合同 | N05 方法路由 | 前者达到状态即记录终点，命令后验不延长；后者先执行命令再记录完成，无环 |
| ORC-027 L1/L4 | 生命周期／底噪／密度方法清单及 B/E 全目录 | 参数化绑定 N05 | 每方法使用正确合同与注册 adapter；N06 在 N07 前，未选方法不运行 |
| ORC-028 L1/L4 | N09 清理失败或未知 | N10/T6/F4 | 可交付不完整记录；残留与作用域明确；不宣称环境清理完成 |

### T06. 持久化、竞态与外部副作用

| ID／层 | Given / 注入 | When | Then：精确预期 |
|---|---|---|---|
| ORC-029 L2/L3 | graph receipt 已提交，checkpoint 尚未保存时硬杀 | 原 thread 重启 | 原 receipt 复用，外部启动数不增加，节点继续正确后继 |
| ORC-030 L2/L3 | intent 前／intent 后／submission_started 后分别硬杀 | 恢复 | 未发可证明才提交；不明确进入对账；不盲目重复创建 |
| ORC-031 L3 | 远端创建成功，响应丢失 | 超时后恢复 | 找到同 operation 资源并补记，真实资源数=1；不造原始延迟 |
| ORC-032 L3 | 接口无幂等且无法找回未知请求 | 接管 | uncertain + quarantine，submit 不再发生；独立域可继续 |
| ORC-033 L2 | 两 worker 同时 claim 一个 job | barrier 同时释放 | 只有一个有效 epoch；另一方不能提交结果 |
| ORC-034 L2/L3 | worker_A 失租，B 接管，A 迟到；A 分别暂停在 saver 提交前、pending writes 前 | A 提交业务状态、checkpoint、pending writes 和新派发 | 所有当前写入均拒绝；B 的 checkpoint/interrupt/后继不回退；旧证据可附历史，旧发压许可被拒绝 |
| ORC-035 L2/L3 | 失租时远端请求仍在途 | 冲突任务申请资源 | 域不因 lease 到期立即释放；对账闭合后才放行 |
| ORC-036 L2 | G_A/G_C 共享 host_A；G_B 独立 | 并行调度 | A/C 不重叠，B 可推进；多域领取失败不留下部分锁 |
| ORC-037 L2 | 事件重复、倒序、丢通知、同 seq 冲突 | outbox/inbox 消费 | 重复不重计，缺序等待／补取，扫表找回，同 seq 冲突阻塞该 aggregate |
| ORC-038 L2/L3 | wait 注册前、事务中、提交后分别到达事件 | 停驻与唤醒 | 不丢唤醒、不重复激活，同 thread 无并发执行 |
| ORC-039 L2/L3 | 暂停时数据库断开，runner 已启动 | 时钟推进并恢复 DB | 无新控制面派发；runner 有界截止；恢复先对账，连续性中断标明 |
| ORC-040 L2/L3 | 产物发布后 DB 失败；DB 引用文件丢失 | 恢复／验收 | 前者 orphan 可回收或复用；后者拒收，不补空文件 |
| ORC-041 L3 | N05 运行中硬杀控制面，runner 继续 | 控制面重启 | 接管原 handle；不重发整轮；采样／发压会话身份保持 |
| ORC-042 L3 | runner 或采样器崩溃 | 恢复并完成剩余预算 | 原断续段标 interrupted／insufficient；不拼成完整连续实验 |

ORC-029–032 在以下故障切点逐一执行：保存 intent 前、intent 后、submission_started 后、远端实际生效后但返回前、receipt 提交后但 checkpoint 前、checkpoint 后但发布通知前、交付文件发布后但账本提交前。每个切点都有独立测试结果，不以一次“恢复测试通过”覆盖全部。

ORC-034 还需验证两个线性化顺序：A 的 saver 事务已锁定租约行时，B 的接管不能抢先推进；B 先完成 epoch 更新时，A 的 saver 必须回滚。独立查询 saver 的实际 checkpoint、pending writes 和 interrupt payload；不得只检查业务账本的 revision。变体覆盖正常 checkpoint、异常后的 pending writes、人工 interrupt、子图 namespace；任何不带有效 token 的直接 saver 写入都应被拒绝。

### T07. 等待、交付、版本与收口

| ID／层 | Given / 注入 | When | Then：精确预期 |
|---|---|---|---|
| ORC-043 L1/L2 | 进入人工等待并有活动采样／压力 | 触发 interrupt | 先有界停止与资源处置；WaitDecision 无新副作用；超时不批准 |
| ORC-044 L1/L2 | 两问题、旧版本答案、重复答案与其他问题答案 | resume | 精确匹配 question/interrupt/scope/version；不解锁错误工作；正确重复答复幂等 |
| ORC-045 L2/L3 | 等待期间 owner 退出，保留资源到期 | watchdog 运行 | 依已定合同停止／清理或隔离；不无限续租；状态与证据齐全 |
| ORC-046 L1/L4 | 子节点“成功”文本但 manifest 不符 | T6 二次验收 | 拒绝且给缺口；原 execution 不被改写；不信模型摘要代替读证据 |
| ORC-047 L4 | A 组先交付，B 组仍运行 | 主图处理事件 | 先生成 A 的部分分析／Excel，B 不被取消，主图不等全部组才分析 |
| ORC-048 L2/L4 | 分析中收到组 v2，当前输入固定 v1 | A2–A7 执行 | 本版只用 v1，标 dirty 后新分析用 v2；不混合读取 latest |
| ORC-049 L2/L4 | F4 封存前到达新交付／新计划 | closure CAS | 旧报告不能置当前最终版；保留历史并重算受影响部分 |
| ORC-050 L1/L4 | 有 running、waiting、blocked 未处理项 | R5 收口 | 不得视作全部完成；只有明确 disposition 后允许部分归档 |
| ORC-051 L1/L4 | 已选 unsupported、failed、blocked_closed | 覆盖与报告 | 分母仍含全部已选项；不支持无性能值；失败／阻塞不是 pass |
| ORC-052 L1/L4 | only-analysis／only-report／改 Excel／改文字 | 执行所选阶段 | 分别只调用必要 A/F；无组事件死等；远端测量 submit=0 |
| ORC-053 L0/L4 | 原始数据完整仅计算 bug；完成事件缺失 | R6 返工分类 | 前者重算，后者补证或新测量；返工只含受影响 work，保留旧结果 |
| ORC-054 L0/L4 | 新阈值、新规格、新完成事件三类变化 | diff/reuse | 阈值可重算；新规格新工作；缺事件重测；旧样本身份不改 |
| ORC-055 L1/L2 | 不兼容 graph/state 版本与旧 checkpoint | restore | 拒绝强行解释，指出旧定义与恢复入口；不重新提交副作用 |
| ORC-056 L0/L2/L3/L4 | 图增删后有在途 job／interrupt、共享资源与迟到交付 | 按 D14 激活新版本 | 新 run/thread、旧任务原图收尾、兼容证据显式复用；下列 a–h 均需验证 |
| ORC-057 L4 | 单候选／多候选不可比合同 | A4/F1 | 单候选比较 N/A；不可比保留条件，不造排名、不启用未经要求的总分 |
| ORC-058 L4 | 最终归档缺原始文件；清理存在未知 | F4 | archive incomplete，保留恢复入口；不能因有报告就称完整交付 |

ORC-056 的子用例（均为待实现测试，不是执行结果）：

- **a 身份与恢复：** 新图必须创建不同 run/thread 并绑定谱系；旧 checkpoint、interrupt 只由旧定义恢复。删除旧节点不能删除其清理入口；旧定义不可用明确 blocked。
- **b 派发截止竞态：** 用数据库 barrier 强制两种顺序：先取得提交许可的旧 operation 按在途对账；先关闭 dispatch gate 则旧 claimant 提交被拒。网络调用可晚于切换，但必须有切换前持久许可，副作用次数与 operation 对应。
- **c 激活原子性：** 在事务提交前及提交后/outbox 消费前硬杀 controller；前者无可执行新运行、旧 gate 不变，后者恢复恰好一个后继且旧普通派发保持关闭。
- **d 幂等及并发：** 相同 request_id/输入重放同一结果，改输入拒绝；两个不同候选竞争同一 predecessor revision，仅一个成功，无双后继和重复派发。
- **e 收尾与隔离：** 旧图 drain、取消失败和 unknown 分别注入；新独立域可运行，共享域不得提前启动；旧等待答复不能批准新版本。被替换未启动等待撤销，资源保留到期有明确处置，不以 lease expiry 释放资源。
- **f 迟到与复用：** 旧结果通过独立兼容复核才生成 reuse_from；不改旧 work，不污染已冻结快照；复用候选等待到期后只能明确转重测／blocked。已启动新测量后旧结果不得加入新测计数。
- **g 回退与代码保留：** 回退生成新谱系节点，无 checkpoint 时间倒退、重复外部提交或删除旧失败证据；旧版本不能在必要清理结束前被卸载。
- **h 同图计划变更：** graph_ref 相同时允许 active_plan_ref CAS；原 activation 固定旧输入，新 activation 才读取新计划；原 WorkInstance 不改归属，追加 repeat/generation 与复用引用区分。

### T08. 测量接口的合成回归数据

本节验证图没有改变脚本合同、边界和证据身份；真实发压算法与公式在脚本实现阶段需另做单元／适配测试。相同夹具可以作为两层合同测试的共享输入，但预期输出必须独立保存。

**ORC-059：固定预算和完成槽位，L1/L4。** 合成 Q_total=5、C=2，runner 首批两请求都返回 accepted，完成时间依次为 1、3、2、4、5 秒（按实际分配的操作 ID 保存）。SpyGateway 验证图传入原合同且没有将 accepted 反馈作为 request complete；状态仅从 runner 的完成结果消费。进程中断后传回同预算和原 operation IDs，失败消耗请求额度，不补齐到五次成功。真正“一个完成立即补槽”的时序由 ControlledRunner 的合同测试及后续真实 runner 测试共同验证，图层不假称自己测了补槽精度。

**ORC-060：分批间隔与最后余量，L1/L4。** Q_total=5、B=C=2、gap=2 秒；第一批在 1 秒结束，第二批 3 秒发起、4 秒结束，最后一批 6 秒发起、7 秒结束。图保存 batch_id、phase 与原窗口；整轮成功吞吐为 5/7，最后批次没有额外 gap。活动窗口按明确集合另算，不以剔除 gap 的数值替换主视图。

**ORC-061：每秒成功吞吐、重复和迟到，L1/L4。** 测量窗口 `[0,2.5)`，成功观察事件为 op1@0.2、op1@0.2 重复、op2@0.8、op3@1.2、op4@2.2；另有 op5@2.8 迟到。预期桶计数 `[2,1,1]`，桶时长 `[1,1,0.5]`，速率 `[2,1,2]`；期限内总吞吐=4/2.5=1.6。op5 单列迟到，不混入；op1 后续退出不抹去其历史创建成功，最终密度可为 3。

**ORC-062：采集缺口与批间零值，L1/L4。** 一份 evidence 明确整个空桶采集完整，另一份在相同时间段存在事件序列缺号。前者可零，后者 unknown；日志 internal_duration 单列，不能换算成客户端成功时刻。跨进程 clock epoch 不兼容时不直接相减。

**ORC-063：资源真实分母与 Guest OS 缺口，L1/L4。** 请求5个、实际存活3个，仅观测到其中2个的 PSS。图传递 requested=5、cohort=3、observed_subset=2 及各自身份；允许子集均值、禁止总体均值。Guest OS 归属缺失时净底噪 unavailable，原始 PSS 和密度吞吐仍可交付；不得因此重跑全部环境组。

**ORC-064：角色共置、CPU峰值与来源，L1/L4。** 同一进程因管理／数据角色出现两条映射，使用真实进程身份去重；组件 A 在 t1/t2 为 8/1，B 为 1/8，聚合峰值应为 9 而非16。编排验收必须保留原样本、计算版本和范围，不能把平台整体与组件结果再求和。

### T09. 端到端验收剧本

| ID | 剧本 | 必须可观察的结果 |
|---|---|---|
| ORC-065 | G_A 已安装、G_B 需配置；各跑两个已选 case；B 组较慢 | 五阶段路径正确、组 A 先增量交付、最终统一比较、所有工作都有 owner 和二次复核 |
| ORC-066 | 一个工作有效但性能失败，另一个脚本成功却证据不足，第三个能力不支持 | 三种状态清晰分列、只对缺证据工作补交／补测、总覆盖不缩水 |
| ORC-067 | 两组并行，中途硬杀主 driver；恢复后有一次人工等待和一次清理失败 | 不重复实验、正确接收答案、遗留资源隔离、能输出限定范围归档 |
| ORC-068 | 一轮完成后只改比较规则，再只改报告措辞 | 第一轮原始证据不变；两次新交付都可追溯，第二次不调用测量及统计 |
| ORC-069 | 10,000 条原始样本或大日志，与少量样本使用相同 work 数 | GraphState 仍只含有界引用／摘要；模型输入不随原始日志大小线性膨胀；真实 token 缺失标 unknown |
| ORC-070 | 图执行路由与 registry 全量覆盖审计 | 45 个持久节点均有 handler 合同和测试归属；所有条件出口与共同失败路径至少一例 |

ORC-069 的 10,000 仅为工程压力夹具，不是用户测量规模。测试不规定未经基准测量的毫秒阈值；先验证确定性预算、无 busy loop、线程／任务数上界及零常规模型调用，实际调度耗时基线在实现后建立，独立于沙箱性能报告。

### T10. 需求与节点追溯矩阵

| 需求／节点范围 | 设计章节 | 测试入口 |
|---|---|---|
| WF-01–03、18、31、34；全节点职责 | D03、D06–D07 | ORC-010–017、024–028、070 |
| WF-04、14、17、20–22；三级 agent | D02、D05、D12 | ORC-020–023、046、065、069 |
| WF-05、35；R7/GW 及共同等待 | D09–D12 | ORC-038、043–045、067 |
| WF-06–08、11、15；R0/G0/N01/N09 | D04–D06、D10–D11 | ORC-018–019、028–042、053–055 |
| WF-09–10、28–30、32、36；R1/RP/R2/R3/R6 | D08–D10、D14 | ORC-001–010、033–038、052–056 |
| WF-12–13、16、19；T6/TC/TB/GB | D06、D12–D13 | ORC-005–006、016–017、022、046、050–051、057 |
| WF-23–27、33；A1/A2/A3/A4/A5/A7/F1/F2/F4 | D13、D15 | ORC-047–054、057–058、065–068 |
| ENV-01–10、15–19、21；E1/E2 | D07–D08 | ORC-011–014、046、065 |
| ENV-11–14、20、23–24；C1/C2/C3 | D06–D07、D11 | ORC-015、024–025、030–032、065 |
| ENV-22、RT-19–21、31–32；N04/N06 | D07、D11 | ORC-025、027、042、063–064 |
| RT-01–08、18、23、33、35；T0/T1/T2/T4/T5 | D07–D08、D10–D11 | ORC-001–006、015、027、036、070 |
| RT-09–17、22、24–30、34、36–37；N00–N10 | D06–D07、D11 | ORC-016–018、024–028、041–042、059–064 |
| RT-40–42；来源产物与创建端点身份 | D17、D11 | ORC-071–073 |
| T8/GE、所有复核与发布 | D09、D13 | ORC-037–040、046–051、065–068 |
| R4/R5/R8/RX、E3、T3/T7、GM/GD、A6、F3 | D07、D13 | ORC-002、011、015、024、046–058、070 |

本矩阵表示编排层追溯，不声称覆盖所有产品行为。RT 生命周期内容正确性、ENV 安装命令实际效果和真实资源精度还需要 L5 的实际适配测试。方法目录 B00–B10/E01–E06 的通用节点绑定由 ORC-027 参数化覆盖。

### T11. 属性测试与故障验收

对以下输入做有限种子属性测试：重复／乱序事件、选测组合、状态更新序列、两 worker 的 claim/renew/expire 顺序、receipt/checkpoint 崩溃切点。检验 I01–I14，不用复制生产实现作为 oracle。

必备变异反例：将 accepted 当 complete、把 `+1` 作为完成计数、删除 fence 校验、将 lease expiry 当资源释放、让 A1 读取 latest、跳过 N09、将 wait timeout 当批准、把缺测补零、把 `guest_ready` 与 `state_ready` 合并、声明 router 但无管理面仍放行。对应测试必须失败，证明断言能捕获目标错误。变异检查针对关键协议，不为了覆盖率生成无意义测试。

异常分类测试需覆盖重试可成功、预算耗尽、永久失败、unknown-effect 和 performance-fail；只有 TRANSIENT_READ 等明确可重试错误自动有限重试。回归测试在新问题出现时追加最小反例，不无条件反复跑所有无关层。

### T12. 准入、完成标准与交付格式

Q3 已确认，实施分组见 [tasks.md](tasks.md)；未回答的真实测试条件仍保持 blocked，不影响合成 fixture 的编排测试。实施计划中每组实现同时落地对应层测试，不把所有测试推迟到最后。

编排验收完成至少要求：

1. 本文件全部适用测试有实际结果；ORC-056 的 a–h 全部执行，不能以设计已确认代替通过。
2. 45 节点映射、所有合法／失败出口、作用域及覆盖不变量通过；不以行覆盖率代替业务证明。
3. 正式后端上的双执行者、硬崩溃和持久恢复测试通过；仅内存测试不够。
4. 受控 runner 的副作用次数与账本一致，未知效果被隔离，没有对其他组资源的越权清理。
5. 交付包包含 machine-readable 测试报告、case→requirement 映射、版本、seed、日志及失败证据，明确 skipped/blocked。
6. 真实沙箱适配验收状态单列；编排通过不能被改写为 Cube/Kuasar/AgentENV 性能测试通过。

建议结果记录：`test_id / level / fixture_hash / requirement_refs / design_refs / versions / outcome / assertions / artifact_refs / blocked_reason`。未来命令与脚本路径在实现后固化；本设计不把尚不存在的 pytest 文件或 CLI 当成已经可执行的验收命令。

### T13. 创建端点与来源身份的契约测试（D17）

对应 D17、BCD-12／BCD-13 与 BCT-09／BCT-10；图只验证字段绑定、校验收口与可比性键，不重算脚本内算法。oracle 用显式字面量，不调用被测归约／路由函数生成预期。

| ID／层 | Given / 注入 | When | Then：精确预期 |
|---|---|---|---|
| ORC-071 L0/L1 | 声明 router 但 `management_plane_present=false`；声明 `co_located_cli` 但 `readiness_signal!=ready_fd`；B01. 缺 `artifact_kind` | N00 preflight／端点解析 | 全部 `blocked`；不静默回退 node_direct、不填默认 kind；远端拓扑出现 `ready_fd` 被拒 |
| ORC-072 L1/L4 | 同方法下两条样本：一条 `state_ready`（黑盒）、一条 `guest_ready`（白盒）；另有 router 同步样本 `api_complete==state_ready` | N08/T8 分析与分组 | 白盒与黑盒分属不同比较键、不并入同一分位数；router 同步样本不额外产生第二次就绪延迟 |
| ORC-073 L1/L4 | 同一“模板”参数解析为 `image_artifact+cold_boot` 与 `snapshot_manifest+memory_restore` 两变体；`B01.SNAPSHOT.DISK` 与 `.MEMORY` | 选测编译与 WorkKey 生成 | 两种来源与端点产出不同 `method_id`／`condition_hash`，不合并；未解析来源标 `unverified` 且不作已知值参与比较 |

ORC-071–073 只提升编排层合同覆盖，不声称 BCD-12／BCD-13 的脚本算法或真实产品语义已验证；后者由 benchmark 契约测试与真实适配验收分别证明。

### T14. 测试间环境还原的图编排契约测试（D19）

对应 D19、WF-37／RT-43 与 BCD-14／BCT-11。oracle 用假列表／假 gateway，不调用被测函数生成预期。

| ID／层 | Given / 注入 | When | Then：精确预期 |
|---|---|---|---|
| ORC-074 L1/L4 | 账本已登记部分资源，另有未入账的孤儿沙箱（同 run_id）；预热与基线实例共存 | N09／run-scope cleanup | 孤儿被扫描删除并入清理证据；预热／基线不被删；逐资源与扫描结果并集进入 `cleanup_status` |
| ORC-075 L1/L4 | 扫描后仍有一个同 run_id 实例残留（或单删失败） | N09 → closure | `cleanup_status=incomplete`，`ClosureService` 判 `partial`，保留残留身份与恢复入口 |

层级与目录：`tests/orchestration/unit/`（图侧）与 `tests/benchmark/unit/`（脚本侧）分开；oracle 独立、不共用生产归约。受控通过只提升代码契约状态，不声称真实环境已验证。

## Risks / Trade-offs

- fake runner 可以掩盖真实 API 不幂等 → L3 明确模拟 unknown，L5 再验证产品关联能力。
- 时序测试容易抖动 → 用 barrier 与故障切点，真实超时只测边界所需部分。
- 模型解释质量不能靠结构校验完全证明 → 测试验证事实边界与权限，语义复核仍保留独立人工／模型职责。
- 只测最终成功会漏掉失败历史 → 所有验收查询原始 attempts、events 和资源账本，禁止仅看最后状态。
- 尚无实现 → 本轮文档校验与设计审阅不得登记为 ORC 测试已通过。
