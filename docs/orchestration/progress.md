# 实施记录 — sandbox-evaluation-workflow

用户在 2026-10-21 明确要求设计后直接 apply。规划为26组96任务，未经真实验收不勾选。设计独立审阅两项已修正：自然结束统计包含最后事件；原生fork按child原子预留预算。审阅复核无重要缺口。

- 当前工作区已有未提交规划和代码草稿，按用户“就在这里”及自主实施要求原地工作，保留全部历史文件，不自动commit/push。
- Python3.14/HarmonyOS；.venv安装锁定LangGraph、pytest、psycopg等成功，实际import通过。
- 基线：45 tests collected，tests/test_runner.py因sandbox_bench.runner不存在而collection error（原有草稿缺口），将按新合同实现该入口。
- PostgreSQL服务、Docker及真实沙箱环境目前未提供；正式持久集成与真实Linux/沙箱验证不以模拟代替。
- 分工按文件边界执行；编排、测量计算、负载／场景分别由独立代理实现与复核，主代理负责合同、适配、持久本地账本及跨组件集成。

## 本轮实现进展

- 已实现 LangGraph 五类图、持久 job/activation/receipt、提交许可与 fencing、等待／恢复、图版本新谱系切换、独立组交付及冻结分析输入。正式 PostgreSQL 仓储和 saver 已编码，尚无真实服务端验收证据。
- 已实现版本化 CaseContract、三产品协议适配、四种完成终点、WINDOW/BATCH/有限开环、生命周期与密度执行、持久 parent/child/stage 意图与结果、PSS/CPU/稳态/Guest 减项合同和实际指标公式。
- 已接 BenchSession N00–N10 与 D11 RunnerGateway；失败进入停止观测与清理。全失败且缺规格证据不再误报 valid；源恢复未知与采样未落盘保留缺口。
- `bindings.RegisteredBenchmarks` 将固定模板与已展开 work 绑定，T2 仅构造注册会话，拒绝未注册／变更的合同，不从模型输出导入代码。
- 已接 A3/A4/A7/F1/F4：冻结交付物化、原始证据复算、横向可比性判断、实际 Excel、事实报告及同分析版本归档。未知在途、未处置缺口与新证据使归档阻塞；明确失败或隔离只能进入部分归档。
- 本地硬退出测试覆盖提交意图、外部效果、child receipt 三个切点。它验证本地文件执行账本，不替代 PostgreSQL 多 worker 硬杀验收。
- 独立审查已驱动修复：Driver lease 释放与精确 interrupt resume、T6/GE 冻结版本交付、closure CAS、pending sampler spool、迟到资源和清理后旧 seal 复用、clone 源恢复、原始输出校验和未知副作用预算。

## 当前验证边界

最终全库原始输出、逐测试JUnit、代码摘要、严格spec及链接检查统一保存在 [验证记录](../verification/README.md)。并行 TDD 中间结果不作为最终验收结论。完整任务的勾选与代码已部分实现分别登记。

本轮后续已接：B08有预算独立恢复和unknown资源对账；单宿主来源引用与N09/N10；基于实际测量合同的accepted复用及原样本去重；有限已注册重算派发。独立审查复现并修正B08后验丢失、恢复时间污染性能窗口、借入源引用释放、归档处置跨run、复用review重放冲突等问题，均加入回归。

仍需明确保留：真实 PostgreSQL 并发／断连验收，真实 Linux procfs 权限与节点部署，三产品线上最小功能／性能验证。共享build、快照产物大小和完整源副作用、snapshot/image删除和物理释放、远端命令未知对账、补样／重测执行器等仍有代码缺口，不能归咎于仅缺环境。完整清单见统一验证记录、requirement-coverage和实施报告；tasks只按整条验收勾选。


## 环境适配 change（2026-10-21）

另一个在途 change [`implement-sandbox-environment-adapters`](../../openspec/changes/implement-sandbox-environment-adapters/tasks.md) 目前为 **22/65 tasks**。已落地的是环境检查/配置链路依赖的通用底层模块，不是完整环境检查或环境配置产品能力。三产品 adapters、产品级安装/调参/修复、环境 LangGraph/CLI 接线和真实部署验收仍未完成；详细边界见 [环境适配说明](../environment-adapters.md)。
