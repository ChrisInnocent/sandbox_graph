# 生命周期能力与验证边界

更新：2026-10-24。当前实现位于项目根，没有本轮提交或推送。[连续补齐记录](../verification/agent-lifecycle-runtime/remaining-20261024.md) 记录当前门禁；[父任务](../../openspec/changes/sandbox-evaluation-workflow/tasks.md) 和[环境任务](../../openspec/changes/implement-sandbox-environment-adapters/tasks.md) 按完整验收条件标记。历史增量的“待实现”不能作为当前清单。

| 范围 | 已交付代码 | 验收边界 |
| --- | --- | --- |
| 生命周期循环 | 作用域/失败观察/双层预算、修复策略、原操作对账、恢复计划、强制收尾、原子 outbox | 本地服务与真实 StateGraph 测试；PG 双连接、硬杀 harness 已有，专用 DSN 未提供 |
| 节点与团队 | E/C、R/G、A/F/work 监督；env/eval worker 装配 investigator、coder/reviewer、门禁与候选清理 | 外部 provider/宿主 bridge 尚未集成验收；没有隐式调用宿主 dispatch |
| 编码迭代 | 候选 Git/CAS、独立修订与评审、有限反馈循环、原 ID 取消和隔离对账 | bwrap 实现及独立 canary harness 已交付；缺受验 rootfs/隔离合同，不宣称已验证 |
| 门禁 | 固定 required nodeids、controller pytest 三阶段对账、六 gate 外部套件装配、GateManifest 身份绑定、10 类可重复变异 | 开发结果 local_only；部署者须提供并冻结实际外部 oracle/命令。装配清单不等于生产 G0–G6 验收 |
| 晋级 | gate/review/授权 CAS、bundle、ready worker、冻结后继、G6.post、失败回退、从已验 bundle 新解释器启动 worker | 真实发布/回退未验收；PG repair/promotion/rollback/outcome 硬杀测试已交付，未实跑 |
| 环境安装 | 三产品角色/制品/鉴权 recipe、grant/journal、原操作恢复、canary/报告、离线 helper bootstrap、镜像加载、受管容器原 ID 替换、部分修复闭包 | 新装/重入/多机 harness 已交付；实际产品部署未验收。Cube one-click 输入失效不能通过只修 staging 或重放全量 install 假成功 |
| 资源与参数 | 三产品动态 PID/归属、实际 cohort、N04–N07 刷新、PSS/CPU；ublk 当前值/配置加载、参数源码覆盖表 | 需真实产品核对归属；没有直接运行观测入口的参数只证明 startup binding，返回 unsupported，不伪造 effective 值 |
| 宿主原语 | 显式 SSH 身份前后核验、Linux proc/systemd/KVM/PSS 只读验收；ARM 不套用 x86 flags，KVM 打开错误分类 | 缺 `SANDBOX_GRAPH_TEST_HOST_PRIMITIVES` 合同未实跑；设备可打开不等于已经成功启动 guest |

运行中改变代码或图必须生成候选版本，不能改写旧 checkpoint。候选失败可在原预算内继续修改；未知效果、清理与旧版本 drain 保留原身份。生产部署的隔离、provider、数据库、产品测试是分别验收的能力，不存在一个开发绿灯解锁所有自动晋级的开关。
