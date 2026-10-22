# 测试场景详细规格设计

本文件把**可选指标矩阵**（[runtime-evaluation RT-44](specs/runtime-evaluation/spec.md)、[benchmark-design.md](benchmark-design.md) BCD-15）与**可调背景条件矩阵**（RT-45、BCD-16）合并到每个测试场景，形成**逐场景可执行规格**。测量节点链与候选映射见 [test-nodes.md](test-nodes.md) §4–§8；来源产物语义见 [alignment.md §8/§10](../../../docs/source-audit/alignment.md)。

**通用约定**
- 指标/条件用 ID 引用：**R=需测/需声明**、**O=可测/可选**、**—=不适用**。
- 完成判据为四终点 `API_COMPLETE / STATE_READY / SERVER_TERMINAL / EXEC_COMPLETE`（+ 白盒 `GUEST_READY`），逐用例声明；默认 `STATE_READY`。
- 不适用／未采集标 `not_applicable`／`unavailable`，**不得填零**。
- 成熟度：本文为设计与离线契约；未跑真实环境前 `environment_verified=false`。
- **可选择性**：每个场景可按 `Selection` 轴（环境组/沙箱类型/场景/方法/优先级/拓扑/来源 kind/排除/阶段）被选或排除（WF-28–30、WF-38）；自动补齐**必要依赖**但不扩为其它场景性能测试；场景内的 N00–N10 子节点**不可单独摘选**；新增能力/改依赖属**图版本变更**，不是选择。

---

## 1. B00 来源产物构建
与 B01–B10 实例操作并列的独立目录；衡量构建产物本身的一次性成本，不创建待测实例。

### B00.TEMPLATE_BUILD
- **定义**：从镜像／基础产物构建模板产物（异步 job 或转换）。
- **方法身份**：`producer_step=build_job`、`cost_scope=build_included`、产物 `artifact_kind ∈ {image_artifact, sandbox_artifact}`。
- **指标**：R `B-01、B-04、B-06、R-01、R-02`；O `T-03、T-04、S-01–S-05、B-03、B-05、A-01、A-02`（`B-02` 仅并发构建条件）；— `T-01/T-02/T-05、H-*、C-*`。
- **条件**：R `N-03 背景沙箱密度、W-02、SP-01、D-01、D-04`；O `D-02/D-03、W-01、TL-01/02、EP-01`；— `C-01–C-05、L-01/02、N-01/02/04`。
- **完成判据**：构建作业 `ready`／`error`；产物 ID 在返回成功前持久化。
- **正确性断言**：产物 kind 与 digest 入账；Kuasar builder 必须 `kind=='img'` 且具 `e2b-img-` 前缀。
- **失败重入**：register/trigger 前写 `phase=intent`，回执写 `phase=receipt`；丢失写响应不重放，按 handle 对账。
- **边界**：一次性构建成本只计一次，不计入 B01/B02 创建时延（BCD-12 §12.6）。

### B00.SNAPSHOT_BUILD
- **定义**：从已有模板／镜像产物构建可再创建的快照产物。
- **方法身份**：`producer_step=capture_publish`／`build_job`，产物 `artifact_kind=snapshot_manifest`。
- **指标/条件**：同 B00.TEMPLATE_BUILD（`D-04=snapshot_manifest`）。
- **正确性断言**：产物 `snapshot_manifest`；与「从运行实例捕获」（B08）边界分明，不互相顶替。
- **失败重入**：同上；`B00.SNAPSHOT_BUILD ≠ B08`。

---

## 2. B01 从模板／快照创建

### B01.TEMPLATE
- **定义**：固定 `artifact_kind`（`image_artifact`／`sandbox_artifact`）× 启动路径 `cold_boot`，请求显式规格，创建新实例。
- **指标**：R `T-01、T-02、T-05、H-03、R-01–R-06`；O `T-03、H-01/02、S-01–S-07、A-05`；— `C-*、B-*`。
- **条件**：R `N-01、C-01或C-02、SP-01、D-04、W-02、EP-01/02`；O `C-03/04/05、SP-02/03、W-01、TL-01–04、DEP-01、N-04`；— `L-01/02、N-02/03`。
- **完成判据**：四终点分列；默认 `STATE_READY`。
- **正确性断言**：新实例 ID；`artifact_kind` 与 launch path 入账；已取得 ID 只观察不重创建。
- **失败重入**：未拿到 ID 先对账；拿到 ID 不重复创建；历史实例只能用于恢复观察／后验。

### B01.SNAPSHOT.DISK
- **定义**：来源 `snapshot_manifest`，启动路径 `disk_restore`（允许进程重启，不声称内存连续）。
- **指标/条件**：同 B01.TEMPLATE，另加 `A-01 产物大小`、`A-05 恢复一致性`；`D-02/D-03` 需声明。
- **正确性断言**：磁盘状态继承证据（文件 hash、应用版本）；`D-03 快照链深度` 记录。

### B01.SNAPSHOT.MEMORY
- **定义**：来源 `snapshot_manifest`，启动路径 `memory_restore`。
- **指标/条件**：同 B01.SNAPSHOT.DISK，`EP-02` 可含 `GUEST_READY`。
- **正确性断言**：进程内 token 与内存内容连续（不得从磁盘重建伪造）；恢复期峰值另列。

---

## 3. B02 从镜像创建

### B02.FULL
- **定义**：固定 OCI digest，候选所需派生产物**尚未准备**；从镜像准备/转换/发布到启动的完整路径。
- **指标**：R `T-01、T-02、T-04、H-03、R-01–R-06、A-02`；O `T-03、T-05、B-01、B-04、S-01–S-07`；— `C-*`。
- **条件**：R `N-01、C-01/02、SP-01、D-01/D-04、W-02、EP-01`；O `D-02/03、TL-*、N-04`；— `L-01/02`。
- **完成判据**：准备、拉取、构建、发布、启动分列；到所选实例完成点。
- **失败重入**：按 build／job／artifact ID 对账；为复测复现已完成构建须新建条件而非当原实验重试。

### B02.PREPARED
- **定义**：合法镜像派生产物**已存在**，只计实例启动路径。
- **指标**：同 B02.FULL 减去准备阶段 `T-04`（或单列）。
- **正确性断言**：命名为「已准备镜像产物启动」；不得替代 FULL 的完整路径结果。

---

## 4. B03 暂停
- **定义**：`B03.PAUSE`；profile 显式区分保留内存检查点／磁盘保留冷恢复／仅冻结。
- **链**：`PREPARE_RUNNING_STATE → RECORD_PROGRESS_MARKERS → PAUSE_SUBMIT → WAIT_PAUSED → OBSERVE_STOP_AND_RESOURCES → VERIFY_RESUMABILITY`。
- **同步性**：**三产品均为同步**（审计 [alignment §11](../../../docs/source-audit/alignment.md)）：暂停接口返回即 `paused`，`API_COMPLETE` 即 `STATE_READY(paused)`，**不需轮询**；Kuasar router 暂停是**转发**到 node（与 create 的 park 不同）。
- **指标**：R `T-01、T-02、R-05`；O `H-03、S-01–S-05、A-03`；— `T-05、B-*、C-*`。
- **条件**：R `N-01、N-04、SP-01、D-02`；O `C-01、TL-01/03`；— `C-02/03、W-*、L-*`。
- **正确性断言**：实际 `paused`（非仅“不响应”）；`ErrSandboxStarting`(409) 不当成功；执行停止与资源释放分别观测。

---

## 5. B04 恢复

### B04.MEMORY
- **定义**：已暂停 fixture → 恢复 → 所选完成 → 首次有效工作；进程内 token 与文件 hash 双验。
- **同步性**：**Kuasar 异步**（`Connect` 仅受理，须轮询到 `running`，`api_complete_s` 与 `state_ready_s` 分列）；**Cube／AgentENV 同步**（`cubelet.Create` 阻塞／*ready when returns*）。审计 [alignment §12](../../../docs/source-audit/alignment.md)。
- **指标**：R `T-01、T-02、T-05、R-05、R-06`；O `H-03、S-01–S-05、A-05`；— `B-*、C-*`。
- **条件**：R `N-01、N-04、SP-01、D-02、D-03、EP-02`；O `W-02、TL-01`；— `C-02/03/04、L-*`。
- **正确性断言**：token 保留且不能靠磁盘重建；API／状态／首次工作时间分开。

### B04.DISK
- **定义**：`B04.DISK`；磁盘冷恢复，允许进程重启但不声称内存连续。
- **正确性断言**：文件 hash 正确；不得以新进程从文件回显伪造内存连续。

---

## 6. B05 克隆

### B05.FULL
- **定义**：父操作与 child operation 分层；capture/publish/fanout 分段；FULL 源继续运行合同含恢复成本。
- **同步性**：**Kuasar 无原生 fork**（`B05.NATIVE` unsupported）→ 组合 capture+create（成本记 B05.FULL）；**Cube `fork` 阻塞 fan-out**（`results.len()==count`）；**AgentENV fork 待实证**。审计 [alignment §12](../../../docs/source-audit/alignment.md)。
- **指标**：R `T-01、T-02、H-03、R-01–R-06`；O `S-01–S-07、A-03、A-04`；— `B-*、C-*`（除密度 clone）。
- **条件**：R `N-01、C-01、C-05、SP-01、D-02/03`；O `C-03/04、TL、W-02`；— `C-02、L-*`。
- **正确性断言**：child 新身份与隔离写入；源／兄弟原值不变；HTTP 201 中每项单判；部分失败不重放整个 fork。

### B05.PREPARED / B05.NATIVE
- **PREPARED**：来源为快照的克隆；**NATIVE**：原生 fork 一次产生 K child（原子预留 K child 槽与父请求名额）。
- **Kuasar**：`B05.NATIVE` 明确 `unsupported`。

---

## 7. B06 删除

### B06.RUNNING / B06.PAUSED
- **定义**：`DELETE → 完整 API → 逻辑不存在 → 逐拥有资源释放`。
- **同步性**：**Kuasar 异步**（204 仅受理；`BeginSandboxDelete`→`deleting`→异步 finalizer→`dead`，删除时延=轮询到 gone/dead）；**Cube `sync:true` 同步**；**AgentENV `delete_sandbox().await` 阻塞同步**。审计 [alignment §12](../../../docs/source-audit/alignment.md)。
- **指标**：R `T-01、T-02、R-05、A-03、A-04`；O `H-03、S-01–S-05`；— `T-05、B-*、C-*`。
- **条件**：R `N-01、N-04、SP-01`（RUNNING/PAUSED 分别）；O `C-01、TL-01`；— `C-02/03、W-*、L-*`。
- **正确性断言**：鉴权错误不当 404；共享模板引用不误删；再次删除只属对账，不补首次时延；残留走 A-04（还原扫描）。

---

## 8. B07 命令

### B07.FIRST
- **定义**：首次标记命令；fixture 未执行过验证命令。
- **指标**：R `T-01、T-02、R-06、R-02`；O `T-05、H-03、S-01–S-05`；— `B-*、C-*`。
- **条件**：R `N-01、SP-01、L-01、EP-02`；O `C-01、TL、W-02`；— `C-02/03/04、D-*`。
- **正确性断言**：bytes／hash／exit 全验；非零预期单列。

### B07.STEADY / B07.OUTPUT / B07.ERROR
- **STEADY**：稳态命令吞吐与延迟；**OUTPUT**：输出完整性；**ERROR**：非零退出／错误路径单列，超时查远端进程，不重发未知命令。

---

## 9. B08 快照

### B08.LOCAL / B08.PUBLISHED
- **定义**：实际触页＋文件标记 → capture → 必要 publish → 后验独立 restore。
- **同步性**：**AgentENV `capture_snapshot().await` 同步**；**Kuasar `export` 要求先 pause，同步性待实证**；**Cube 等 Master，待实证**。审计 [alignment §12](../../../docs/source-audit/alignment.md)。
- **指标**：R `T-01、T-02、A-01、A-02、R-05、R-06`；O `T-04、S-01–S-05、A-04、A-05`；— `B-*、C-*`。
- **条件**：R `N-01、SP-01、D-02、D-03、W-02`；O `TL、C-01、DEP-01`；— `C-02/03、L-*`。
- **正确性断言**：本地／远端就绪分开；源状态副作用记录；逻辑与新增物理大小分列，无证据不推算。

---

## 10. B09 底噪

### B09.PLATFORM / B09.IDLE / B09.PAUSED
- **定义**：零实例／空闲／暂停基线，用于密度外推与净开销。
- **指标**：R `S-04、S-05、S-06、R-05`；O `S-01–S-03、A-03、A-04、C-04`；— `T-*（除准备）、B-*`。
- **条件**：R `N-01、N-04、SP-01、DEP-01、TL-01`；O `W-02、TL-02、L-01`；— `C-02/03/04`。
- **正确性断言**：底噪以 PSS 为基础；净底噪剔除 Guest OS 缺证明时不可用而原始 PSS 保留（Q12）；暂停卸载零值须有生命周期对账依据。

---

## 11. B10 密度

### B10.GROWTH_LIST / B10.GROWTH_READY
- **定义**：两种口径——存量净增长（LIST）与选中成功数（READY）**独立停止状态机**。
- **指标**：R `C-01、C-02、C-03、H-05、S-04–S-06、R-01–R-05`；O `H-01–H-04、C-04、A-04、S-01–S-03、S-07`；— `B-*`。
- **条件**：R `N-01/N-02、C-01、C-03、C-05、SP-01、TL-01/03、DEP-01`；O `C-02/04、N-03/04、W-01/02、TL-02/04、L-01`；— `L-02`。
- **正确性断言**：净增长不替换新增健康；旧实例损失与不完整列表可识别；不运行官方全局 reset。

### B10.PROJECTION / B10.FIXED_REQUESTS.WINDOW / B10.FIXED_REQUESTS.BATCH / B10.SLO_VERIFY
- **PROJECTION**：两公式、整数 MiB 兼容取数、锁定 awk 参考舍入；每值带模型/窗口版本与非实测标签。
- **FIXED_REQUESTS**：固定 `Q_total`（非 N），失败消耗预算；分母同窗口；WINDOW 与 BATCH 分别声明 `C-03/C-04`。
- **SLO_VERIFY**：不创建；要求显式比较方向；无阈值则 `blocked`。

---

## 12. E01–E06 低优先级扩展

| 场景 | 定义 | 需测指标 | 需声明条件 |
|---|---|---|---|
| E01.WORKLOAD | 沙箱内负载执行性能 | T-01/T-02、H-03、S-01–S-05 | L-01、SP-01、N-01 |
| E02.RESET | 快照回滚／状态重置 | R-05、A-05 | D-02/03、SP-01、N-01 |
| E03.MIXED_SOAK | 混合生命周期与长期运行 | T-01/T-02、H-03、S-01–S-05、C-05 | N-01/N-02、TL-01、SP-01 |
| E04.PRESSURE | 资源弹性与压力下运行 | S-01–S-05、C-01–C-03 | C-01、N-02、SP-01 |
| E05.INTERFERENCE | 共置负载干扰 | S-01–S-05、T-01/T-02、H-03 | L-02、N-01、SP-01 |
| E06.RECOVERY | 故障后恢复性能 | R-01–R-06、T-01/T-02 | L-01、TL-01、SP-01、N-01 |

扩展均**可测/可选**，默认不执行；纳入计划后才细化工作负载、条件与验收（RT-03）。

---

## 13. 场景 × 规格 覆盖与成熟度

- 每个场景的**指标 R/O/—** 见 BCD-15；**条件 R/O/—** 见 BCD-16。
- 测量节点链（N00–N10）与候选映射见 [test-nodes.md](test-nodes.md) §4–§8。
- 当前成熟度：设计+离线契约已就绪；**B00 构建期资源、产物大小、冷热缓存未实现**（BCD-15 `A-01/A-02/B-03/B-04/B-05` 为 ❌），**B00 背景密度条件未建模**（BCD-16 §16.3 `N-03`），对应 `tasks.md` 组 36/37。
