# 测量代际本地门禁

2026-10-23，用户明确要求停止 Superpowers 和 agent 审查，改为 TDD 与确定性测试门禁。本批采用该验收方式；没有把中止的复审记作通过。

运行入口（使用项目虚拟环境的 Python，在工作树根目录）：

```bash
python scripts/check_measurement_generation.py
```

实际结果：176 passed in 13.30s，门禁退出码0，JUnit 176项，0失败、0错误、0跳过。入口固定运行9个相关测试文件，非零退出、空结果、缺失/损坏JUnit、失败/错误/跳过均不通过；每次保留独立日志、JUnit和summary。

覆盖：旧代不可重开、断段/fixture失效保持insufficient、原operation接管、有效性能fail不触发重测、授权撤销/到期与提交先后顺序、版本任务阻止归档、过期版本报告取消、失败第二代经新授权进入第三代且沿用原预算、来源/owner/授权错误拒绝，以及原sampler桥接回归。

TDD记录：授权准入新增反例4项先失败；版本任务/报告5项先失败；第三代入口正例在缺API时先失败，修复后通过并补齐拒绝路径。修复覆盖5个生产文件和2个测试文件；本门禁另新增一个固定执行入口。

45.3本地代码验收完成。真实PostgreSQL竞争、远程沙箱/物理sampler部署仍未验收；T6及多作用域由45.4/45.5继续承担。此脚本是本地回归入口，不是47组候选发布门禁。

原始证据：[日志](generation-local-gate-20261023-pytest.log)、[JUnit](generation-local-gate-20261023-junit.xml)、[命令和结果](generation-local-gate-20261023-summary.json)。
