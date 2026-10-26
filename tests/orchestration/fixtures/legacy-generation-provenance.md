# 旧 generation 消费者的固定源码

来源为本仓库提交 `2dfe8aee`，用于离线验证新 envelope/job kind 被旧消费者拒绝。测试运行不读取 Git、控制器快照、父 checkout 或绝对工作路径。

| fixture | 原文件及 SHA-256 | 保存规则及 fixture SHA-256 |
|---|---|---|
| `legacy_scheduling_2dfe8aee.py.txt` | `sandbox_graph/services/scheduling.py`，`5856ccb2ec90cbf064678729b262fc84a23425c4fc266084b2d4caa6dc502889` | 全文件原始 bytes；同左 |
| `legacy_worker_2dfe8aee.py.txt` | `sandbox_graph/runtime/worker.py`，`34761f45032924f88cd3adf1807e7a8f5d873a67ba60a3b67b964eedd3f870bb` | 用 `ast.get_source_segment` 逐字保留顶层 imports 和 `WorkerPolicy`、`Worker` 两个 class，以双换行连接并加尾换行；`a82335cb451616985ae875e9c82abff9fd2179a140940715419a2f61ad5332e0` |

这些是未修改行为的历史生产类，不是模仿旧版本的手写 mock。测试以原 package 名解析其相对 imports，依赖当前仓储/协议接口；结论只针对这些旧消费者拒绝扩展的路径，不代表整个旧二进制集群或数据库迁移验收。测试显式校验 fixture hash；损坏或缺文件应失败，不以 skip 掩盖。

`legacy_measurement_plan.json` 是改动前原 `PlanCompiler` 产生的旧格式 Plan 原件。独立固定摘要为 `a561d2ac65552dd5e519bc81eace37a81142ffa7c5d363d4616e5c5c9c7f9d0f`；测试同时校验其 wire bytes 和 Plan.hash，防止默认字段改变旧摘要。
