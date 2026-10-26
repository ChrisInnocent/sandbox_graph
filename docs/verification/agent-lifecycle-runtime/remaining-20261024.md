# 连续补齐与验证记录

本页对应 2026-10-24 用户“所有非环境阻塞需求继续补齐”的连续工作。没有提交或推送；此前证据快照计数不改写。本页记录本轮代码与本地验证交付；不代表真实后端和所有上游能力均已验收。

已实现：eval 新验收传递 bootstrap helper_paths；原操作私有日志分块导出；三产品动态 PID 归属、Kuasar 父链、MMDS 字节/总时限与 N04–N07 刷新接线；Cube/AgentENV 部分安装依赖闭包、受管容器原 ID 替换、离线镜像加载；ublk 当前值/配置/无强卸载激活；新解释器 bundle-worker；门禁执行结果必须服从独立清理对账。

本地定向证据：环境与相关部署/采样回归 316 passed、7 个现场合同缺失 skip；后续 ublk/recipe/registration/变异校验 28 passed；候选运行/可信门禁 23 passed；采样 wiring 18 passed（6 subtests）。集合重叠，不相加；完整最终回归待本轮冻结。

`artifacts/reviews/remaining-20261024/mutations-complete/summary.json`：固定 ATG-06 变异 **10/10 被捕获**。覆盖去 fence、未知 usage 计零、fixture 跳 N05、无限重试、自签 gate、无 ready worker 激活、候选/修复失败即终止、性能失败自动重采、零收集判 pass。在临时代码副本变异，固定测试保持摘要，只有 setup/teardown 成功而 call 失败算捕获；收集失败、skip、xfail、超时不算。命令 `python scripts/verify_critical_mutations.py --output 新目录`。这是开发反例证据，不能签发生产 GateManifest。

新增 `test_publication_hardkill.py` 的 15 个真实 PG/SIGKILL 切点，覆盖修复 order/job/request、晋级/回退绑定与 commit 两侧、外部效果→receipt→outcome/结算/outbox。独立文件账本不允许重复动作覆盖。时间来自 PostgreSQL，接管等待真实 lease 过期。当前 **15 skipped**（无专用 DSN），两个真实时钟 epoch 的种子合同测试通过；不能据测试代码存在称数据库硬杀已验证。

当前外部套件装配、源码参数观测覆盖、任务状态与使用文档已同步。部署专用 oracle/隔离命令尚未提供，不能据装配机制存在声称已具备完整生产黑盒套件。真实产品、PG、隔离、provider 验收仍分别缺显式运行合同；观测能力限制另列，不与环境缺失混淆。

可信套件装配补齐：六 gate 外部套件真实 collect、摘要/路径/required nodeid 拒绝及 prepared/unverified 输出，6 项定向通过。它不代替部署专用黑盒 oracle 的内容及隔离命令；不能据装配成功宣称 G0–G6 已通过。运行参数覆盖已形成源码索引；本 profile 无直接运行观测的字段明确 unsupported，不作为安装故障反复修复。


后续同轮交付：Cube embedded VMM 安装前置与 one-click 输入失效依赖、ARM/KVM 错误分类、EAT-30 只读 SSH/宿主原语 harness、Kuasar 可选独立 APISecret 引用与私有物化。42 项 Cube/源清理回调定向通过；宿主相关 17 项、原语合同 10 passed/1 live skipped、Kuasar 鉴权/组部署 18 项通过。集合重叠，不相加。

另补 8 项 PostgreSQL saver 两种锁顺序 × checkpoint/pending writes × 同步/异步竞争，观察实际 pg_blocking_pids；该文件共 10 skipped，缺专用 DSN，不声称竞争已验证。当前冻结 profile：development 2529、persistent 60、isolation 1、release 3。

先前全仓轮次 3186 passed / 2 failed / 60 skipped；两处失败分别是 10ms 测试假设下 delete callback 尚未启动，以及本机偶发 exec 的 EINTR。前者改为等待真实回调开始后中断，后者单项复跑通过，未给安装副作用增加自动重试。最新全仓与固定门禁结果待下方实际回执更新。


## 最终本地验证

- 全仓：`root-regression-final.xml`，**3199 passed / 69 skipped / 110 subtests passed**，393.95 秒，无失败。此轮开始收集之后新增的独立 APISecret 两项用例已由下方最终固定门禁覆盖，不将两组计数相加。
- 固定 development：`development-final/summary.json`，**2529/2529 pass**，errors=[]，零 skip；真实 collect/run/JUnit/phase 对账及执行前后输入摘要一致。执行 288.14 秒；13 条 LangSmith 的 Python 3.16 弃用提示，不是测试失败。
- ATG-06：`mutations-complete/summary.json`，十类实际变异全部 caught。
- `persistent-final`、`isolation-final`、`release-final`：均明确 blocked，分别缺专用 DSN、隔离合同、DSN+产品合同，没有执行真实后端测试或默认连接。
- 三份 change strict 和文档引用校验 valid；`git diff --check` 无错误。最后任务勾选/记录属于上述门禁之后的文档更新，代码及固定用例未再修改。

以上 artifact 均位于 `artifacts/reviews/remaining-20261024/`，保留 collection、原日志、JUnit、阶段回执与 profile/输入摘要；开发门禁不签发生产 GateManifest。

## 剩余项目的精确分界

| 类别 | 未完成项 | 缺少的条件 / 已有代码 |
| --- | --- | --- |
| 真实 PG | 父 1.1、3、4、11.2、14.1、43、48.1、49.2；环境 4.5/8.6 的完整验收 | 专用 DSN；仓储/saver/grant/事务/预算/硬杀 harness 已交付，60 项 persistent 必需用例已冻结 |
| Linux/产品/采集 | 父 26.2/26.3、31.4、32.5、33.5、34.4、35.4、36.4、37.4、38.3、40.4、41.7、50.7/50.9；环境 10.1–10.4/11.4；对齐 4.1–4.3 | 实际节点、权限、制品/模板、SSH/产品测试合同；原语、安装、功能、资源和测量 harness 已有，尚无本轮真实测量 |
| 隔离/晋级 | 父 46.2/46.5、47.2、48.2/48.3、49.3 | 受验 rootfs、controller suite/固定隔离命令、真实 worker/发布环境；bwrap/取消/对账/bundle 启动/晋级代码已交付，不以候选输出自证生产安全 |
| Provider | 父 49.4 | 实际选定 provider 或宿主 bridge 的端点/合同/权限；持久请求/query/cancel/usage/context 代码和受控反例已有 |
| 上游观测能力 | 环境 5.4/6.4/7.4 的完整有效值验收 | 配置解析/修复/启动绑定/已暴露运行值/ublk 均有代码；未暴露直接值的 limiter/pool/scheduler 字段明确 unsupported。**这不是仅缺机器，也不是能由本项目伪造值填平的缺口**；新增直接证明需上游观测接口及新审计 profile |
| Kuasar 导入 | 环境 6.5 的完整验收 | 私有 source generation、CAS、同版本 ManifestKey/APISecret 引用解析与 inline 物化已有；实际导入/节点下发/canary 未验收。上游直接 secret ref delivery 明确未配置，不能谎报支持 |

生产黑盒套件的部署专用 oracle 内容仍须由受信维护者冻结；本轮交付了完整装配与独立执行机制，没有把装配器冒充已经具备全部部署专用断言。因此没有将所有未勾选任务概括成“只差虚拟机”。

## 2026-10-26 提交前状态纠正

仍有两类非环境实现未完成，分别由父 tasks 54.1/54.2 与环境 tasks 14.1/14.2 跟踪：通用生产可信黑盒断言/固定观察脚本，以及部分参数的真实运行值探针。之前将具体 oracle 一概归为部署者输入的表述不充分；本项目仍需交付通用内容。当前 profile 缺探针也不能直接等价于上游完全不存在观测接口，应逐字段确认并实现。

2026-10-24 的全仓与固定门禁是对应快照的历史证据；提交前发现 operation_sync 及对应测试已发生后续修改，增量测试另记，不冒充已经重跑当前整个工作树。提交包含现有实现、设计、任务与上述未完成状态；真实后端和上游能力限制保持分列。
