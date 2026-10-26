# 2026-10-24 继续实施记录

工作区为项目根目录。保留原有未提交资产；本轮未提交、推送或改动三个产品源码目录。任务跟踪采用 OpenSpec apply，未重新使用 Superpowers 或委派审阅 agent。

## 已落地的代码

- 生命周期操作：rollout 的 off/actions/scoped/candidate、精确 run/scope/node 权限与 CAS、不可变请求审计；关闭新增工作仍允许安全预算内的原操作清理。rollback CLI 复用发布事务，等待 ready worker 时也保存原请求，不能以相同 ID 改回退目标。
- 候选执行：应用内 coder/reviewer、独立上下文、真实 CAS 编辑与 revision 冻结；Git 导出限制时间/字节/成员数，最小上下文按总字节裁剪；隔离执行在校验 rootfs 后、启动进程前重验权限。worker 配置失败不遗留连接池。
- 可信观察：controller-pytest.v1 在候选外运行固定 collect/run、逐阶段报告、JUnit 和日志对账；拒绝可被其他身份写入的 suite 根目录、缺 case、skip、摘要变化和候选自报通过。候选附加测试独立执行，不能覆盖受信失败。原隔离请求仍独立对账和清理。
- Kuasar 集群：私有 JSON generation 原子发布，原 source/新 generation 复核，placer 配置 CAS、固定官方配置校验及 restart。已有记录与 source 保留，冲突不隐式轮换。prepare_kuasar_groups 脚本绑定私有 APISecret 版本，Adapter 临时签发 MAC 令牌；没有把 ManifestKey 直接当 APISecret。
- 管理角色：当前 boot、产品服务、启动配置独立探测，再关联组控制入口与原 canary。声明 group_dependency 证据层级，不把 registry/placer/scheduler 当计算节点查询，不虚构每进程请求轨迹。功能失败仍须清理原实例，旧 boot 证明不可复用。
- 资源映射：cgroup 边界与 sandbox/generation 规则、实际 PID/start_ticks/boot、collector_procfs 快照输出；未匹配/歧义/缺 PSS 保留缺口。
- 大日志账本：超过 64 KiB 的终态结果独立持久化并以摘要引用，预算/事件更新不再反复写入原文；重入校验后展开，封存仍保留原始日志。两个真实进程退出切点覆盖“证据已落地但终态未提交”和“终态已提交”，不释放原预算或重复外部动作。
- Kuasar 部分安装：补回二进制/配置/unit 后按已声明角色重新校验并启动或重启依赖服务；unit 补回先 daemon-reload，无关角色不重启。这不扩展 AgentENV setup/容器重建及 Cube 一键安装的可重放范围。

## 已执行验证

这些集合有重叠，不相加。各命令均使用项目 `.venv/bin/python`。

| 范围 | 实际结果 | 日志或 JUnit |
| --- | --- | --- |
| 上轮根目录完整回归快照 | 3079 passed、44 skipped、110 subtests passed；不是本轮最终结果 | `/tmp/sandbox-full-final.log` |
| 环境模块阶段回归 | 255 passed、6 skipped | `artifacts/reviews/continuation-20261024/environment.log` |
| 候选工作区/可信 pytest/编码 worker | 32 passed | `artifacts/reviews/continuation-20261024/candidate-bounds.log` |
| 后续 group 漂移/脚本/CLI/隔离启动 | 35 passed | `artifacts/reviews/continuation-20261024/focused.log` |
| 44.4、45.5、46.3、48.4 定向合同 | 72 passed | `artifacts/reviews/continuation-20261024/task-evidence.xml` |
| 本轮首次根目录回归 | 3104 passed、1 failed、44 skipped、110 subtests passed；失败为大日志账本热路径超时 | `/tmp/continuation-full.log` |
| 大日志图与 session/账本恢复 | 32 passed；N00—N10 及分析归档、原文/摘要隔离通过 | `/tmp/journal-evidence-green.log` |
| 部分安装依赖与账本中断 | 33 passed，包含两个新增进程退出切点 | `/tmp/repair-journal-green.log` |
| 修复后根目录完整回归 | **3112 passed、44 skipped、110 subtests passed**，632.93 秒 | `artifacts/reviews/continuation-20261024/root-regression-fixed.xml`、同目录 `.log` |
| 固定开发门禁 | **2441/2441，pass，errors=[]，local_only**；收集、阶段回执、JUnit 及执行前后输入摘要一致 | `artifacts/reviews/continuation-20261024/development-fixed/summary.json` |

主 change 在以上四项完整本地验收后更新为 **179/225**。完整可信生产隔离、实际 provider 与 PG 验收仍由 49.2–49.4 单列。环境任务 6.5 已有子交付，但当前上游不支持 ref 下发、现场下发验收未满足完整条件，仍不勾选。

新增功能先运行失败测试，再补实现。worker pool 测试第一轮被测试替身不支持库泛型导入阻断，随后修正夹具；该导入错误不是生产行为的有效 TDD 失败证据。其余相关定向回归和最后35项组合已通过。

本次大日志失败保留在 `root-regression.xml`；修复后的全量报告另写 `root-regression-fixed.xml`，不覆盖失败证据。定向反例日志为 `/tmp/journal-bounds-red.log`（2 failed）与 `/tmp/partial-repair-red.log`（4 failed）。修复不增加请求超时；大日志合成负载的 5 次请求全部成功，负载阶段约 1.8 秒，仅用于回归诊断，不作为沙箱性能结果。一次定向命令误指不存在的 `test_recipe_catalog.py`，未执行测试；后续使用实际测试路径，不能将该次命令计作通过。

44 个 skip 对应已有 Linux procfs、三产品目标、PostgreSQL、真实隔离与产品端到端合同缺口；未增加隐藏 skip。OpenSpec 三个 change 严格校验及规划引用校验通过（`planning-fixed.json`）；`git diff --check` 通过。固定 profile 更新为 development 2441、persistent 37、isolation 1、release 3；开发门禁通过，其他三个入口仍保留各自缺环境合同的 blocked 记录。固定开发门禁是本地验证，不是生产 GateManifest。

## 仍需完成的代码与外部验收

该次快照结束时，代码工作仍有：远端 helper bootstrap 与离线制品投递（含 Cube 镜像投递）、真实产品动态 sandbox/cgroup/generation 归属发现及刷新、部分角色的运行时有效参数探测、完整 remediation/promotion 的 PG 硬杀矩阵及发布失败回退组合 harness。此外仍须补齐 G0—G6 标准可信黑盒套件、AgentENV setup/容器变更等部分安装恢复合同、远端私有日志受限导出及 bundle 启动部署入口。这些不标成环境阻塞，也不声称全部工作完成。

外部验收条件分别为专用 PostgreSQL、具备所需 namespace/capability 的隔离环境、明确的三产品单机/多机目标及 provider 合同。本机缺少对应条件时只记录 blocked/skip；QEMU 或普通单元测试不替代产品部署验收。当前主任务仍在实施。

后续已补齐 helper bootstrap 与离线资产投递；实现及新增验证见 [bootstrap 实施记录](bootstrap-20261024.md)。镜像投递不等于 Docker load 或产品安装，本页历史测试计数保持原样。

上述为历史快照；后续代码、故障矩阵及反例门禁的最新状态见 [连续补齐记录](remaining-20261024.md)。
