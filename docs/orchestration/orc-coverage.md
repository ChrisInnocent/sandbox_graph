# ORC 覆盖账本（ORC-001–073）

2026-10-21 生成。来源：`orchestration-test-design.md` T04–T09。
状态：`tagged`=测试显式标注；`functional`=由同一节点/模块测试按功能覆盖；`blocked`=需真实 PostgreSQL/产品/特权环境。

| ORC | 设计期望（截断） | 直接映射测试 | 状态 |
|---|---|---|---|
| ORC-001 | 结果为显式预期集合 `(A∪B)∩M−X`；不产生其他 tuple | — | blocked |
| ORC-002 | 分别为配置错误／空计划／无匹配；远端 submit=0 | test_registry.py | tagged |
| ORC-003 | 只增加创建／暂停准备和验证；不增加 B01/B03 性能用例 | — | blocked |
| ORC-004 | 给出受影响 B04 与冲突原因；不覆盖排除 | — | blocked |
| ORC-005 | 保留两个覆盖项；分别 TC/TB；无虚假耗时和零分 | — | blocked |
| ORC-006 | 仅产生建议或 RP 差异；扩展 submit=0 | — | blocked |
| ORC-007 | 拒绝静默使用新集合；返回重验／新预览原因 | — | blocked |
| ORC-008 | 拒绝并定位依赖；历史 graph 不变 | — | functional |
| ORC-009 | 合法；不能把全部控制流当 DAG；预算耗尽可退出 | — | blocked |
| ORC-010 | 静态拒绝；避免双分支执行 | — | blocked |
| ORC-011 | 分别 T0／C1／GB；未知不安装；在位路径 C2=0 | — | blocked |
| ORC-012 | 产出差异报告与范围归档；C2=0，不转性能测量 | — | blocked |
| ORC-013 | 不制造多环境组；不因单机就安装管理组件 | — | blocked |
| ORC-014 | 单列访问断言；公钥配置／安装 submit=0 | — | blocked |
| ORC-015 | 前者 blocked 不提交；后者 C3 拒绝放行，不进依赖测量 | test_work_preflight.py | tagged |
| ORC-016 | execution=succeeded、data=insufficient、review=rejected；不填 pass | test_transitions.py | tagged |
| ORC-017 | data=valid、performance=fail；正常出结论，测量启动仅一次 | — | functional |
| ORC-018 | 原失败保留；新 attempt 关联原因；新 repeat 真执行不被旧 receipt 吞掉 | test_models.py | tagged |
| ORC-019 | 重复幂等，冲突报错，旧版本不覆盖当前；完成数不增加 | — | blocked |
| ORC-020 | 调度／采样／统计分支 SpyModel 调用=0；F1 模型策略单独验 | test_agent_context.py | tagged |
| ORC-021 | 每节点仅 own scope；不含其他组日志、原始秘密及全量对话 | test_observability.py | tagged |
| ORC-022 | 拒绝转执行事实；未注册 submit=0；已有合法记录不变 | test_agent_proposals.py | tagged |
| ORC-023 | 预算内分页或受限失败；不静默截掉关键失败；usage 缺失=unknown | test_agent_budgets.py | tagged |
| ORC-024 | 禁止后续测量；保存已有证据；均尝试 N09 并交付缺口 N10 | test_work_measurement.py | tagged |
| ORC-025 | 有权复位后才采空基线，否则 blocked；不得基线污染后放行 | test_observation_protocol.py | tagged |
| ORC-026 | 前者达到状态即记录终点，命令后验不延长；后者先执行命令再记录完成，无环 | test_measurement_contract.py | tagged |
| ORC-027 | 每方法使用正确合同与注册 adapter；N06 在 N07 前，未选方法不运行 | — | blocked |
| ORC-028 | 可交付不完整记录；残留与作用域明确；不宣称环境清理完成 | — | blocked |
| ORC-029 | 原 receipt 复用，外部启动数不增加，节点继续正确后继 | — | blocked |
| ORC-030 | 未发可证明才提交；不明确进入对账；不盲目重复创建 | test_tasks_events.py | tagged |
| ORC-031 | 找到同 operation 资源并补记，真实资源数=1；不造原始延迟 | — | blocked |
| ORC-032 | uncertain + quarantine，submit 不再发生；独立域可继续 | — | blocked |
| ORC-033 | 只有一个有效 epoch；另一方不能提交结果 | test_leases.py | tagged |
| ORC-034 | 所有当前写入均拒绝；B 的 checkpoint/interrupt/后继不回退；旧证据可附历史，旧发压许可被拒绝 | — | blocked |
| ORC-035 | 域不因 lease 到期立即释放；对账闭合后才放行 | test_wait_watchdog.py | tagged |
| ORC-036 | A/C 不重叠，B 可推进；多域领取失败不留下部分锁 | — | blocked |
| ORC-037 | 重复不重计，缺序等待／补取，扫表找回，同 seq 冲突阻塞该 aggregate | — | blocked |
| ORC-038 | 不丢唤醒、不重复激活，同 thread 无并发执行 | test_machine_waits.py | tagged |
| ORC-039 | 无新控制面派发；runner 有界截止；恢复先对账，连续性中断标明 | — | blocked |
| ORC-040 | 前者 orphan 可回收或复用；后者拒收，不补空文件 | test_report_closure.py | tagged |
| ORC-041 | 接管原 handle；不重发整轮；采样／发压会话身份保持 | — | blocked |
| ORC-042 | 原断续段标 interrupted／insufficient；不拼成完整连续实验 | — | blocked |
| ORC-043 | 先有界停止与资源处置；WaitDecision 无新副作用；超时不批准 | — | blocked |
| ORC-044 | 精确匹配 question/interrupt/scope/version；不解锁错误工作；正确重复答复幂等 | — | blocked |
| ORC-045 | 依已定合同停止／清理或隔离；不无限续租；状态与证据齐全 | — | blocked |
| ORC-046 | 拒绝且给缺口；原 execution 不被改写；不信模型摘要代替读证据 | — | blocked |
| ORC-047 | 先生成 A 的部分分析／Excel，B 不被取消，主图不等全部组才分析 | test_incremental_analysis.py | tagged |
| ORC-048 | 本版只用 v1，标 dirty 后新分析用 v2；不混合读取 latest | — | blocked |
| ORC-049 | 旧报告不能置当前最终版；保留历史并重算受影响部分 | — | functional |
| ORC-050 | 不得视作全部完成；只有明确 disposition 后允许部分归档 | — | blocked |
| ORC-051 | 分母仍含全部已选项；不支持无性能值；失败／阻塞不是 pass | — | blocked |
| ORC-052 | 分别只调用必要 A/F；无组事件死等；远端测量 submit=0 | — | blocked |
| ORC-053 | 前者重算，后者补证或新测量；返工只含受影响 work，保留旧结果 | — | blocked |
| ORC-054 | 阈值可重算；新规格新工作；缺事件重测；旧样本身份不改 | test_change_impact.py | tagged |
| ORC-055 | 拒绝强行解释，指出旧定义与恢复入口；不重新提交副作用 | — | blocked |
| ORC-056 | 新 run/thread、旧任务原图收尾、兼容证据显式复用；下列 a–h 均需验证 | — | functional |
| ORC-057 | 单候选比较 N/A；不可比保留条件，不造排名、不启用未经要求的总分 | — | functional |
| ORC-058 | archive incomplete，保留恢复入口；不能因有报告就称完整交付 | — | blocked |
| ORC-059 | — | — | blocked |
| ORC-060 | — | — | blocked |
| ORC-061 | — | — | blocked |
| ORC-062 | — | — | blocked |
| ORC-063 | — | — | blocked |
| ORC-064 | — | — | blocked |
| ORC-065 | 五阶段路径正确、组 A 先增量交付、最终统一比较、所有工作都有 owner 和二次复核 | — | blocked |
| ORC-066 | 三种状态清晰分列、只对缺证据工作补交／补测、总覆盖不缩水 | — | blocked |
| ORC-067 | 不重复实验、正确接收答案、遗留资源隔离、能输出限定范围归档 | — | blocked |
| ORC-068 | 第一轮原始证据不变；两次新交付都可追溯，第二次不调用测量及统计 | — | blocked |
| ORC-069 | GraphState 仍只含有界引用／摘要；模型输入不随原始日志大小线性膨胀；真实 token 缺失标 unknown | — | functional |
| ORC-070 | 45 个持久节点均有 handler 合同和测试归属；所有条件出口与共同失败路径至少一例 | — | functional |
| ORC-071 | ORC-071–073 | test_endpoint_source_binding.py | tagged |
| ORC-072 | 白盒与黑盒分属不同比较键、不并入同一分位数；router 同步样本不额外产生第二次就绪延迟 | test_endpoint_source_binding.py | tagged |
| ORC-073 | 两种来源与端点产出不同 `method_id`／`condition_hash`，不合并；未解析来源标 `unverified` | test_endpoint_source_binding.py | tagged |
