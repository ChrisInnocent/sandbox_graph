# 完整用例文件与条件矩阵

新执行入口使用自包含的 `case.v1` 文件。`sandbox_bench/casegen.py` 只把规格标签和显式矩阵转换成 `CaseContract`，必填字段、单位和方法约束仍由原合同校验；没有隐式基础用例或 CLI 条件覆盖。重复 YAML/JSON 键、未知字段、空轴、重复轴值以及超过 1024 格的矩阵均拒绝。

仓内 `config/case-low.yaml` 是 2v2G、Q=2、C=1 的完整合成用例；`config/case-matrix.yaml` 展开 2 规格 × 2 并发。两者的 source/version/environment 是合成夹具，不能当真实环境合同；其中资源观测显式豁免，只用于验证执行链。

```bash
# 生成每格的完整 JSON 用例及来源证据，不执行测试
.venv/bin/python -m sandbox_bench.runner generate --case config/case-matrix.yaml --output artifacts/generated-cases

# 文件直接进入真实图调度和会话，产品 API 是合成后端
.venv/bin/python scripts/local_mock_run.py --case config/case-low.yaml --out artifacts/mock-case-low
.venv/bin/python scripts/local_mock_run.py --case config/case-matrix.yaml --out artifacts/mock-case-matrix

# 真实单用例：另提供实际后端配置；矩阵必须先显式展开
.venv/bin/python -m sandbox_bench.runner case --case /path/to/complete-case.yaml --config /path/to/adapter.yaml --output /path/to/new-session
```

`spec` 支持 `2v2g`、`2v4`、`8v16G`、`{vcpus, memory_gib}`，以及原生 `{vcpus, memory_bytes}`。`axes` 支持 `specs/concurrency/requests/repeat_ids`，按该顺序稳定展开；每格拥有独立 work 输入身份。必须保留基础用例的必填字段，不能依靠某个轴补齐残缺合同。

`GeneratedCase.register(...)` 生成计划 Case 与 `RegisteredBenchmarks` 模板。能力验证状态、冲突域和优先级由部署侧显式提供，生成器不会把产品能力变成 verified。图只重绑 work/plan 身份；方法、规格、负载、repeat 等测量条件保持文件值。模型不参与这些转换。

输入文件 SHA256、生成用例 SHA256、测量合同摘要、全部条件及其摘要进入 session config、封存 manifest 与 comparison 元数据。会话创建前重验条件，文件改动产生不同模板/计划身份；数据缺口依旧保持 unavailable。`generate` 另写 `.input.json` 保存原文件来源；生成的 JSON 本身也是可独立运行的完整合同。

兼容入口 `conditions.v1` / `--profile` 仍保留给旧的合成示例。它不是完整测试合同；不应使用其未接线的描述字段声明真实测试。需要修改方法、终点、密度、资源或重复次数时，使用上面的完整用例入口。

下文保留旧 profile 格式供既有脚本兼容查阅。

## 旧条件 profile

把测试条件（目标单节点密度、创建并发、沙箱规格、请求量…）从脚本硬编码中解耦到
**外部文件**，以便：
- 整体调到很低（例如单组、2 沙箱、并发 1、`2v2g`）来验证**图工程/循环工程**是否打通；
- 之后快速调整/放大测试条件，不改代码。

加载器：`sandbox_bench/conditions.py`；示例档：`config/*.yaml`。

> 红线：profile 只描述**条件**，不产出**结论**。不得由 profile 推导测量默认值，
> 也不得用它填补缺失证据（缺数据仍记 `unavailable`）。未知字段会被**拒绝**，不静默忽略。

## 用法

```bash
# 低条件全链路跑图（离线 mock：真图/真调度/真证据 + 合成后端）
.venv/bin/python scripts/local_mock_run.py --profile config/conditions.yaml

# 矩阵（规格 × 并发 × 请求），轴写在 profile 的 axes 下
.venv/bin/python scripts/matrix_run.py --profile config/matrix-small.yaml
```

## 字段

| 字段 | 类型 | 含义 |
|---|---|---|
| `schema_version` | `conditions.v1` | 版本 |
| `profile` | str | 档名（写进产物便于追溯） |
| `groups` | 正整数 | 环境组数（默认 1，验证用） |
| `works_per_group` | 正整数 | 每组 work 数 |
| `target_density_per_node` | 正整数? | **目标单节点沙箱密度**（真实密度场景用） |
| `spec` | `NvMg` 或 `{vcpus, memory_gib}` | **沙箱规格**，如 `2v2g`/`2v4g` |
| `concurrency` | 正整数 | **创建并发** |
| `max_work_concurrency` | 正整数 | work 级并发上限 |
| `requests` | 正整数 | 请求量 Q |
| `repeat` | 正整数 | 重复轮次 |
| `methods` | 字符串数组 | 方法集（如 `[B01.TEMPLATE]`） |
| `topology` | str | 端点拓扑（`node_direct`/`router`/…） |
| `completion_endpoint` | str | 完成终点（`STATE_READY`/`API_COMPLETE`/…） |
| `fail_last_group` | bool | 注入最后一组失败（循环/修复验证用） |
| `axes.specs` / `axes.concurrency` / `axes.requests` | 数组 | 矩阵轴；为空则单格 |
| `notes` | str? | 说明 |

规格标签 `NvMg`：`2v2g` = 2 vCPU / 2 GiB，`2v4g` = 2 vCPU / 4 GiB。
解析严格（`2x2g`/`0v2g` 等一律拒绝）。

## 与既有条件轴的关系

profile 里的字段对应 `scenario_spec.CONDITION_FIELDS` 的字段路径（如
`C-01 → load.concurrency`、`SP-01 → spec`），并进入 `CaseContract`。矩阵轴与
[benchmark-matrix.md](benchmark-matrix.md) 的「场景 × 规格 × 并发 × 请求 × 条件」一致。

## 低条件验证档示例（`config/conditions.yaml`）

```yaml
schema_version: conditions.v1
profile: verify-low
groups: 1
works_per_group: 2
target_density_per_node: 2
spec: 2v2g
concurrency: 1
max_work_concurrency: 1
requests: 2
methods: [B01.TEMPLATE]
```
