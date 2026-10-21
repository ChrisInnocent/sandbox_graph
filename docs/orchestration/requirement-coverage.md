# 主变更逐要求覆盖账本

2026-10-21。每行区分控制面代码、当前测试和未交付业务，不据本地ORC测试把ENV/RT真实能力标完成。RT条目已补实际sandbox_bench实现与测试映射；局部验证仍不等同整项真实产品验收。最终命令与未完成项见[统一验证记录](../verification/README.md)。

| 要求 | 编排实现入口（sandbox_graph/） | 当前验证 | 状态与缺口 |
|---|---|---|---|
| WF-01 五阶段工作流 | graphs/factory.py + services/workflows.py | graphs/test_workflows.py | partial：五类图可构造，A/F真实业务接口已接；双组合成Worker五阶段已测，PG及完整变体待验 |
| WF-02 明确且可追溯的测试合同 | domain/models.py + catalog/registry.py + graphs/executor.py | unit/test_models.py + graphs/test_work_graph.py | partial：有通用结构/节点责任，详尽业务合同与整体验收未完成 |
| WF-03 动作级执行责任 | domain/models.py + catalog/registry.py + graphs/executor.py | unit/test_models.py + graphs/test_work_graph.py | partial：有通用结构/节点责任，详尽业务合同与整体验收未完成 |
| WF-04 主 agent 与节点 sub-agent 职责 | domain/models.py + catalog/registry.py + graphs/executor.py | unit/test_models.py + graphs/test_work_graph.py | partial：有通用结构/节点责任，详尽业务合同与整体验收未完成 |
| WF-05 人工确认的具体性与授权复用 | runtime/waits.py + runtime/driver.py | graphs/test_driver.py + test_human_wait.py | partial：本地interrupt与答复验证通过，持久PG待验 |
| WF-06 运行状态可持久恢复 | persistence/repository.py + checkpointer.py + runtime/driver.py | persistence/test_postgres.py | blocked：真实PG/硬杀未运行 |
| WF-07 外部任务恢复与幂等 | runtime/runner.py + worker.py | unit/test_runner_protocol.py + test_worker.py | partial：本地对账/清理合同通过，产品恢复另验 |
| WF-08 局部重试与重跑 | interfaces/service.py + runtime/worker.py | unit/test_services.py | partial：分析/报告/恢复命令可派发；retry新attempt执行策略待补 |
| WF-09 图节点与依赖增删改查 | graphs/factory.py + interfaces/service.py | graphs/test_work_graph.py | partial：版本定义/环/清理校验已测，全面CRUD矩阵待补 |
| WF-10 变更影响与执行版本 | services/revisions.py + planning/changes.py | unit/test_runtime_ledger.py + test_review_closure_agents.py | partial：激活核心已测，正式PG竞态待验 |
| WF-11 按动作语义复用结果 | services/reuse.py + sandbox_bench/bridge.py + workflow_services.py | unit/test_reuse_projection.py + tests/benchmark/integration/test_analysis_graph.py | partial：producer参数绑定、自动引用交付、原raw复验与覆盖去重已本地验证；真实PG待验 |
| WF-12 执行结果与数据有效性分离 | domain/models.py + transitions.py | unit/test_models.py | locally_verified core：分离状态/终结不可复活/性能不重试 |
| WF-13 证据关联与模型事实边界 | runtime/agents.py | unit/test_review_closure_agents.py | partial：提案边界已测，真实模型连接未验 |
| WF-14 上下文与产物按需读取 | artifacts/store.py + runtime/agents.py | unit/test_artifacts.py | partial：作用域/range预算已测，部署认证/租户边界待补 |
| WF-15 失败清理与不完整运行归档 | graphs/factory.py + runtime/worker.py + services/closure.py | graphs/test_work_graph.py + unit/test_worker.py | partial：失败清理路径已测，F4独立核对已接，真实PG最终seal竞态待验 |
| WF-16 比较与结论的适用范围 | domain/models.py + catalog/registry.py + graphs/executor.py | unit/test_models.py + graphs/test_work_graph.py | partial：有通用结构/节点责任，详尽业务合同与整体验收未完成 |
| WF-17 工作流效率与被测性能分别记录 | runtime/agents.py | unit/test_review_closure_agents.py | partial：model usage记录，统一trace/耗时指标尚未集成 |
| WF-18 各阶段执行职责映射 | domain/models.py + catalog/registry.py + graphs/executor.py | unit/test_models.py + graphs/test_work_graph.py | partial：有通用结构/节点责任，详尽业务合同与整体验收未完成 |
| WF-19 基础选型优先与场景功能验证 | domain/models.py + catalog/registry.py + graphs/executor.py | unit/test_models.py + graphs/test_work_graph.py | partial：有通用结构/节点责任，详尽业务合同与整体验收未完成 |
| WF-20 按环境组派发与隔离上下文 | services/scheduling.py + runtime/worker.py | unit/test_scheduling.py + test_worker.py | partial：独立派发/资源冲突已测，真实并发PG待验 |
| WF-21 分层状态维护与交付复核 | services/reviews.py + workflows.py | unit/test_review_closure_agents.py + graphs/test_workflows.py | partial：追加复核/gap已测，GroupDelivery精确子版本/复核/终态处置已闭合并测试 |
| WF-22 节点自检与环境组分析 | domain/models.py + catalog/registry.py + graphs/executor.py | unit/test_models.py + graphs/test_work_graph.py | partial：有通用结构/节点责任，详尽业务合同与整体验收未完成 |
| WF-23 主 agent 侧统一数据分析 | services/reviews.py + workflows.py | graphs/test_workflows.py | partial：冻结/重验已测，实际analysis/comparison服务已接，见../analysis-report.md |
| WF-24 Excel 结果交付与追溯 | graphs/factory.py | graphs/test_workflows.py | partial：实际comparison Excel导出已接A4，A7发布report job；真实环境数据未验 |
| WF-25 结论生成与脚本化交付 | services/workflows.py + closure.py | unit/test_review_closure_agents.py | partial：事实版本/CAS核心已测，实际报告渲染已接，跨组件证据见../analysis-report.md |
| WF-26 归档清单与恢复入口 | services/closure.py | unit/test_review_closure_agents.py | partial：partial/CAS已测，F4范围/文件与seal事务内在途核对已接，真实PG竞态待验 |
| WF-27 数据分析阶段的横向对比 | planning/changes.py | unit/test_review_closure_agents.py | partial：实际compare_cases已接，方法可比性与真实环境另验 |
| WF-28 可选择的测试执行范围 | planning/compiler.py | unit/test_planning.py + test_services.py | locally_verified core：有限选择/授权/同hash启动 |
| WF-29 依赖感知的执行计划 | planning/compiler.py | unit/test_planning.py | partial：必要准备依赖/排除冲突；复杂依赖DAG矩阵待补 |
| WF-30 选择计划的版本、恢复与覆盖 | interfaces/service.py + services/revisions.py | unit/test_services.py + test_runtime_ledger.py | partial：版本/CAS核心；全覆盖报告需业务链 |
| WF-31 逐节点自动化职责与实现状态 | domain/models.py + catalog/registry.py + graphs/executor.py | unit/test_models.py + graphs/test_work_graph.py | partial：有通用结构/节点责任，详尽业务合同与整体验收未完成 |
| WF-32 主图与组调度的确定性职责 | services/workflows.py + scheduling.py + runtime/worker.py | graphs/test_workflows.py + unit/test_worker.py | partial：确定性派发/消费/等待已测 |
| WF-33 分析和报告的确定性工作 | domain/models.py + catalog/registry.py + graphs/executor.py | unit/test_models.py + graphs/test_work_graph.py | partial：有通用结构/节点责任，详尽业务合同与整体验收未完成 |
| WF-34 节点内部图与共同执行协议 | domain/models.py + catalog/registry.py + graphs/executor.py | unit/test_models.py + graphs/test_work_graph.py | partial：有通用结构/节点责任，详尽业务合同与整体验收未完成 |
| WF-35 等待与诊断前的资源处置 | runtime/waits.py + agents.py + worker.py | unit/test_runtime_ledger.py + test_worker.py | partial：期限处置/unknown隔离；真实撤压由runner验证 |
| WF-36 图版本激活与旧任务收尾 | services/revisions.py + runtime/worker.py | unit/test_runtime_ledger.py + test_worker.py | partial：新run/gate/drain核心；正式竞态/所有旧wait处置待验 |
| ENV-01 固定格式的环境声明 | services/environment.py + runtime/runner.py | graphs/test_environment.py | partial/blocked：通用探测/配置/复检路由已测；具体真实环境脚本与权限、部署历史未验证 |
| ENV-02 已有环境信息的发现与补全 | services/environment.py + runtime/runner.py | graphs/test_environment.py | partial/blocked：通用探测/配置/复检路由已测；具体真实环境脚本与权限、部署历史未验证 |
| ENV-03 期望状态与观测状态分离 | services/environment.py + runtime/runner.py | graphs/test_environment.py | partial/blocked：通用探测/配置/复检路由已测；具体真实环境脚本与权限、部署历史未验证 |
| ENV-04 连通性与初始认证检查 | services/environment.py + runtime/runner.py | graphs/test_environment.py | partial/blocked：通用探测/配置/复检路由已测；具体真实环境脚本与权限、部署历史未验证 |
| ENV-05 自动化密钥配置与验证 | services/environment.py + runtime/runner.py | graphs/test_environment.py | partial/blocked：通用探测/配置/复检路由已测；具体真实环境脚本与权限、部署历史未验证 |
| ENV-06 凭据与日志分离 | services/environment.py + runtime/runner.py | graphs/test_environment.py | partial/blocked：通用探测/配置/复检路由已测；具体真实环境脚本与权限、部署历史未验证 |
| ENV-07 按类型和角色解析检查要求 | services/environment.py + runtime/runner.py | graphs/test_environment.py | partial/blocked：通用探测/配置/复检路由已测；具体真实环境脚本与权限、部署历史未验证 |
| ENV-08 节点属性与有效配置检查 | services/environment.py + runtime/runner.py | graphs/test_environment.py | partial/blocked：通用探测/配置/复检路由已测；具体真实环境脚本与权限、部署历史未验证 |
| ENV-09 安装状态分支 | services/environment.py + runtime/runner.py | graphs/test_environment.py | partial/blocked：通用探测/配置/复检路由已测；具体真实环境脚本与权限、部署历史未验证 |
| ENV-10 检查与配置职责边界 | services/environment.py + runtime/runner.py | graphs/test_environment.py | partial/blocked：通用探测/配置/复检路由已测；具体真实环境脚本与权限、部署历史未验证 |
| ENV-11 安装配置执行合同 | services/environment.py + runtime/runner.py | graphs/test_environment.py | partial/blocked：通用探测/配置/复检路由已测；具体真实环境脚本与权限、部署历史未验证 |
| ENV-12 选型测试配置约束显式化 | services/environment.py + runtime/runner.py | graphs/test_environment.py | partial/blocked：通用探测/配置/复检路由已测；具体真实环境脚本与权限、部署历史未验证 |
| ENV-13 配置后的独立复检 | services/environment.py + runtime/runner.py | graphs/test_environment.py | partial/blocked：通用探测/配置/复检路由已测；具体真实环境脚本与权限、部署历史未验证 |
| ENV-14 环境级协作与最小功能验证 | services/environment.py + runtime/runner.py | graphs/test_environment.py | partial/blocked：通用探测/配置/复检路由已测；具体真实环境脚本与权限、部署历史未验证 |
| ENV-15 新安装与在位环境统一报告 | services/environment.py + runtime/runner.py | graphs/test_environment.py | partial/blocked：通用探测/配置/复检路由已测；具体真实环境脚本与权限、部署历史未验证 |
| ENV-16 安装历史与在位证据分离 | services/environment.py + runtime/runner.py | graphs/test_environment.py | partial/blocked：通用探测/配置/复检路由已测；具体真实环境脚本与权限、部署历史未验证 |
| ENV-17 步骤证据可追溯 | services/environment.py + runtime/runner.py | graphs/test_environment.py | partial/blocked：通用探测/配置/复检路由已测；具体真实环境脚本与权限、部署历史未验证 |
| ENV-18 环境流程重入与快速复检 | services/environment.py + runtime/runner.py | graphs/test_environment.py | partial/blocked：通用探测/配置/复检路由已测；具体真实环境脚本与权限、部署历史未验证 |
| ENV-19 部分失败与环境放行 | services/environment.py + runtime/runner.py | graphs/test_environment.py | partial/blocked：通用探测/配置/复检路由已测；具体真实环境脚本与权限、部署历史未验证 |
| ENV-20 环境动作执行职责映射 | services/environment.py + runtime/runner.py | graphs/test_environment.py | partial/blocked：通用探测/配置/复检路由已测；具体真实环境脚本与权限、部署历史未验证 |
| ENV-21 环境报告信息完整性 | services/environment.py + runtime/runner.py | graphs/test_environment.py | partial/blocked：通用探测/配置/复检路由已测；具体真实环境脚本与权限、部署历史未验证 |
| ENV-22 资源观测对象准备 | services/environment.py + runtime/runner.py | graphs/test_environment.py | partial/blocked：通用探测/配置/复检路由已测；具体真实环境脚本与权限、部署历史未验证 |
| ENV-23 环境工作节点自动化合同与内部图 | services/environment.py + runtime/runner.py | graphs/test_environment.py | partial/blocked：通用探测/配置/复检路由已测；具体真实环境脚本与权限、部署历史未验证 |
| ENV-24 可执行方案与测量前置隔离 | services/environment.py + runtime/runner.py | graphs/test_environment.py | partial/blocked：通用探测/配置/复检路由已测；具体真实环境脚本与权限、部署历史未验证 |
| RT-01 基础评估优先级 | sandbox_bench/registry.py + sandbox_graph/planning/compiler.py | tests/benchmark/unit/test_contract_models.py + tests/orchestration/unit/test_planning.py | partial：目录/选择/优先级已受控验证，B/E完整三产品合同矩阵未齐 |
| RT-02 十类基础场景目录与指标分配 | sandbox_bench/registry.py + sandbox_graph/planning/compiler.py | tests/benchmark/unit/test_contract_models.py + tests/orchestration/unit/test_planning.py | partial：目录/选择/优先级已受控验证，B/E完整三产品合同矩阵未齐 |
| RT-03 低优先级扩展目录 | sandbox_bench/registry.py + sandbox_graph/planning/compiler.py | tests/benchmark/unit/test_contract_models.py + tests/orchestration/unit/test_planning.py | partial：目录/选择/优先级已受控验证，B/E完整三产品合同矩阵未齐 |
| RT-04 场景条件指标分离 | sandbox_bench/contracts.py + bindings.py | tests/benchmark/unit/test_contract_models.py + test_bindings.py | partial：严格有限条件、模板及轮次绑定已测；实际实验参数需环境合同 |
| RT-05 六类统一指标 | sandbox_bench/metrics/ + analysis.py | tests/benchmark/unit/test_metrics_statistics.py + test_metrics_quality.py + test_analysis.py | locally_verified：独立黄金数验证分位数、吞吐、失败与缺测；真实数据未测 |
| RT-06 条件参数矩阵 | sandbox_bench/contracts.py + bindings.py | tests/benchmark/unit/test_contract_models.py + test_bindings.py | partial：严格有限条件、模板及轮次绑定已测；实际实验参数需环境合同 |
| RT-07 跨方案外部语义对齐 | sandbox_bench/adapters.py + registry.py + completion.py | tests/benchmark/protocol/test_lifecycle_adapters.py + scenarios/test_completion.py | partial：三产品受控协议、能力缺口与完成事件已测，完整部署版本矩阵未验 |
| RT-08 能力支持与测试覆盖分别记录 | sandbox_bench/adapters.py + registry.py + completion.py | tests/benchmark/protocol/test_lifecycle_adapters.py + scenarios/test_completion.py | partial：三产品受控协议、能力缺口与完成事件已测，完整部署版本矩阵未验 |
| RT-09 功能验证先于性能结论 | sandbox_bench/session.py + analysis.py | tests/benchmark/unit/test_session.py + test_analysis.py | partial：功能资格与性能值分开、缺证据不判valid；真实功能预检待验 |
| RT-10 计时事件与恢复后有效工作 | sandbox_bench/completion.py + logs.py + fixture_lifecycle.py | tests/benchmark/scenarios/test_completion.py + unit/test_logs.py + test_session_b08_records.py | locally_verified：API/状态/执行/日志终点与时间域分开；后验不改原性能终点 |
| RT-11 逐请求时延与统计可靠性 | sandbox_bench/metrics/ + analysis.py | tests/benchmark/unit/test_metrics_statistics.py + test_metrics_quality.py + test_analysis.py | locally_verified：独立黄金数验证分位数、吞吐、失败与缺测；真实数据未测 |
| RT-12 正确完成吞吐 | sandbox_bench/metrics/ + analysis.py | tests/benchmark/unit/test_metrics_statistics.py + test_metrics_quality.py + test_analysis.py | locally_verified：独立黄金数验证分位数、吞吐、失败与缺测；真实数据未测 |
| RT-13 资源分层与增量开销 | sandbox_bench/resources.py + sampler.py + steady.py + analysis.py | tests/benchmark/unit/test_resources_procfs.py + test_sampler.py + test_steady_windows.py + test_analysis.py | partial：角色/组件/节点PSS与CPU、稳态和实际cohort分母已测；真实Linux/多节点采集待验 |
| RT-14 密度并发与到达率独立 | sandbox_bench/scenario_density.py + load.py + metrics/ | tests/benchmark/scenarios/test_scenario_density.py + test_load.py + unit/test_metrics_statistics.py | partial：增长双口径、投影、固定Q窗口/批次/吞吐黄金例已测；实际容量及SLO阈值待验 |
| RT-15 单节点密度的条件化结论 | sandbox_bench/scenario_density.py + load.py + metrics/ | tests/benchmark/scenarios/test_scenario_density.py + test_load.py + unit/test_metrics_statistics.py | partial：增长双口径、投影、固定Q窗口/批次/吞吐黄金例已测；实际容量及SLO阈值待验 |
| RT-16 数据来源缓存及准备成本 | sandbox_bench/fixtures.py + adapters.py + contracts.py | tests/benchmark/unit/test_fixtures.py + protocol/test_lifecycle_adapters.py | partial：来源/cache/规格证明门控已测；共享build成本与实际effective-spec证明未齐 |
| RT-17 生命周期状态和产物语义 | sandbox_bench/scenarios.py + adapters.py | tests/benchmark/scenarios/test_scenarios.py + protocol/test_lifecycle_adapters.py | partial：基础生命周期与独立FULL/PREPARED创建已实现；共享build K请求及接管未齐 |
| RT-18 测试设计与执行责任 | sandbox_bench/session.py + bridge.py + sandbox_graph/graphs/factory.py | tests/benchmark/integration/test_work_graph_session.py + test_five_stage_worker.py + test_large_evidence.py | partial：N00–N10确定性顺序/有限上下文/无模型测算已测；正式PG恢复和完整异常矩阵未验 |
| RT-19 沙箱规格作为独立实验条件 | sandbox_bench/fixtures.py + adapters.py + contracts.py | tests/benchmark/unit/test_fixtures.py + protocol/test_lifecycle_adapters.py | partial：来源/cache/规格证明门控已测；共享build成本与实际effective-spec证明未齐 |
| RT-20 基础场景的稳态资源观测 | sandbox_bench/resources.py + sampler.py + steady.py + analysis.py | tests/benchmark/unit/test_resources_procfs.py + test_sampler.py + test_steady_windows.py + test_analysis.py | partial：角色/组件/节点PSS与CPU、稳态和实际cohort分母已测；真实Linux/多节点采集待验 |
| RT-21 按节点角色和组件归属分析资源 | sandbox_bench/resources.py + sampler.py + steady.py + analysis.py | tests/benchmark/unit/test_resources_procfs.py + test_sampler.py + test_steady_windows.py + test_analysis.py | partial：角色/组件/节点PSS与CPU、稳态和实际cohort分母已测；真实Linux/多节点采集待验 |
| RT-22 可配置完成判据与事件时间域 | sandbox_bench/completion.py + logs.py + fixture_lifecycle.py | tests/benchmark/scenarios/test_completion.py + unit/test_logs.py + test_session_b08_records.py | locally_verified：API/状态/执行/日志终点与时间域分开；后验不改原性能终点 |
| RT-23 场景工作节点公共合同 | sandbox_bench/session.py + bridge.py + sandbox_graph/graphs/factory.py | tests/benchmark/integration/test_work_graph_session.py + test_five_stage_worker.py + test_large_evidence.py | partial：N00–N10确定性顺序/有限上下文/无模型测算已测；正式PG恢复和完整异常矩阵未验 |
| RT-24 两类创建路径的用例分层 | sandbox_bench/scenarios.py + adapters.py | tests/benchmark/scenarios/test_scenarios.py + protocol/test_lifecycle_adapters.py | partial：基础生命周期与独立FULL/PREPARED创建已实现；共享build K请求及接管未齐 |
| RT-25 暂停恢复的状态合同 | sandbox_bench/scenarios.py + wiring.py | tests/benchmark/scenarios/test_scenarios.py + unit/test_wiring.py | partial：暂停/恢复状态及guest连续性接口已测，真实内存与磁盘证明待验 |
| RT-26 克隆的完整路径与逐子实例验收 | sandbox_bench/scenarios.py + source_lifecycle.py + source_wiring.py | tests/benchmark/scenarios/test_scenarios.py + unit/test_source_wiring.py | partial：三种克隆、持久child/部分结果/源恢复与共享引用已接；snapshot删除和完整隔离矩阵未验 |
| RT-27 删除的多终点与资源归属 | sandbox_bench/session.py + source_wiring.py | tests/benchmark/unit/test_source_wiring.py + protocol/test_lifecycle_adapters.py | partial：owned清理、unknown不重发、独立absence与借入源保护已测；物理释放未证明 |
| RT-28 命令测试的协议与工作量 | sandbox_bench/transport.py + adapters.py + fixtures.py | tests/benchmark/protocol/test_streaming.py + unit/test_fixtures.py + test_wiring.py | partial：流分片/输出hash/exit/FIRST/codec门控已测；远端命令task未知对账与跨机FIRST未接 |
| RT-29 快照捕获发布与可恢复验证 | sandbox_bench/fixture_lifecycle.py + scenarios.py + session.py | tests/benchmark/unit/test_fixture_lifecycle.py + test_session_b08_records.py + test_source_wiring.py | partial：捕获/发布/独立预算恢复/unknown对账与计时隔离已测；产物大小、完整源副作用及物理清理未齐 |
| RT-30 密度双方法及协议保真 | sandbox_bench/scenario_density.py + load.py + metrics/ | tests/benchmark/scenarios/test_scenario_density.py + test_load.py + unit/test_metrics_statistics.py | partial：增长双口径、投影、固定Q窗口/批次/吞吐黄金例已测；实际容量及SLO阈值待验 |
| RT-31 密度联合资源采集与真实分母 | sandbox_bench/resources.py + sampler.py + steady.py + analysis.py | tests/benchmark/unit/test_resources_procfs.py + test_sampler.py + test_steady_windows.py + test_analysis.py | partial：角色/组件/节点PSS与CPU、稳态和实际cohort分母已测；真实Linux/多节点采集待验 |
| RT-32 基于 PSS 的底噪与 Guest OS 扣除 | sandbox_bench/metrics/resources.py + analysis.py | tests/benchmark/unit/test_metrics_resources.py + test_analysis.py | locally_verified：保留raw PSS，Guest减项需同域同快照Q12；缺真实证明输出unavailable |
| RT-33 扩展场景节点保留 | sandbox_bench/extensions.py | tests/benchmark/unit/test_extensions.py | locally_verified skeleton：六类注册profile/有限动作/补偿已测，未选择不执行，真实扩展合同未提供 |
| RT-34 固定请求预算下的并发与节奏对比 | sandbox_bench/scenario_density.py + load.py + metrics/ | tests/benchmark/scenarios/test_scenario_density.py + test_load.py + unit/test_metrics_statistics.py | partial：增长双口径、投影、固定Q窗口/批次/吞吐黄金例已测；实际容量及SLO阈值待验 |
| RT-35 测试工作节点的脚本化边界 | sandbox_bench/session.py + bridge.py + sandbox_graph/graphs/factory.py | tests/benchmark/integration/test_work_graph_session.py + test_five_stage_worker.py + test_large_evidence.py | partial：N00–N10确定性顺序/有限上下文/无模型测算已测；正式PG恢复和完整异常矩阵未验 |
| RT-36 密度过程同步创建吞吐 | sandbox_bench/scenario_density.py + load.py + metrics/ | tests/benchmark/scenarios/test_scenario_density.py + test_load.py + unit/test_metrics_statistics.py | partial：增长双口径、投影、固定Q窗口/批次/吞吐黄金例已测；实际容量及SLO阈值待验 |
| RT-37 测量节点前置顺序与有效分支 | sandbox_bench/session.py + bridge.py + sandbox_graph/graphs/factory.py | tests/benchmark/integration/test_work_graph_session.py + test_five_stage_worker.py + test_large_evidence.py | partial：N00–N10确定性顺序/有限上下文/无模型测算已测；正式PG恢复和完整异常矩阵未验 |
| RT-38 测试执行代码与独立契约验证 | sandbox_bench/ + tests/fixtures/benchmark/ | tests/benchmark/ + tests/test_runner.py | partial：实际实现/独立黄金oracle/回归已有证据；BTC全量变异/跨产品矩阵与真实实测未齐 |
| RT-39 执行器请求预算与副作用恢复 | sandbox_bench/journal.py + load.py + fixture_lifecycle.py | tests/benchmark/integration/test_journal_kill.py + unit/test_journal.py + test_fixture_lifecycle.py | partial：本地三硬杀切点、预算与未知副作用已测；不能替代PG/controller/远端进程硬杀验收 |

## 归档门槛

L0/L1仅部分用例有本地证据；L2没有PostgreSQL服务，L3组合硬杀未执行；L4双组合成五阶段与实际A3/A4/A7/F1/F4已通过，完整变体与PG故障仍未验；L5真实环境缺合同/连接，未执行。主变更不可归档，不得自动同步为全量已实现能力。后续实现入口为当前change tasks 1–26与[脚本合同清单](script-contracts.md)，不是旧规划中的重复源码审计。
