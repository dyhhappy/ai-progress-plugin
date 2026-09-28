# UAH v2 审查报告（UAH_V2_AUDIT）

> 审查时间：2026-09-22。基线：分支 `p01-human-override`（HEAD `0108a93`），
> 工作区带有大量未提交修改（含 uah/ 子系统与 src/safety 多处，疑为并行会话遗留）。
> 本任务在新建分支 `feature/uah-v2` 上进行，**不回退、不覆盖任何已有改动**。
> 审查范围：完整阅读 `uah/` 全部 26 个源文件、`UAH_ARCHITECTURE.md`、
> `uha.py` 装配链、`src/safety/controller.py`、`src/desktop/hotkey.py`、
> `src/scheduler/executor.py` 的进度/验证钩子，以及全部 docs 报告。

---

## 1. 现状

### 1.1 架构（Phase 1 / 1.1 已落地，质量良好）

```
UHA Executor ──SessionController.on_change()──▶ UhaNativeAdapter（唯一映射表）
                                                   │ 出站队列（非阻塞，满丢最旧）
                                                   ▼
                                      UAH Hub（127.0.0.1:8789，HTTP+SSE，先到先得）
                                          │ GET /state  GET /stream（事件驱动，无轮询）
                            ┌─────────────┴─────────────┐
                            ▼                           ▼
                    EmbeddedHud（进程内）          Desktop HUD（独立进程，tkinter）
```

| 审查项 | 结论 |
|---|---|
| 入口 | `uha.py::_setup()` → `uah_begin_boot()` + `uah_attach()`；HUD 独立入口 `uah/hosts/desktop/__main__.py` |
| UI 框架 | tkinter（GitHub 深色主题 #0d1117/#161b22）；无 tkinter 时降级 text_hud |
| 独立进程 | ✅ HUD 独立进程（launch.py 自动挑带 tkinter 的系统 Python 3.12）；Hub 常驻 daemon |
| 状态获取 | 事件驱动：`SessionController.on_change` + SSE 推送；`render_card()` 是唯一渲染真源 |
| 轮询 | 仅 UI 内部 250ms 队列排水 + 1s 时钟刷新 Label 文本，无状态轮询；Compact 审批轮询 1s（可接受，但有改进空间） |
| 共享变量 | 无跨进程共享变量；跨进程仅 HTTP+SSE |
| CU 状态产生 | `SessionController.phase == COMPUTER_CONTROL`（executor 写）；安全层另有 `SafetyController`（**独立于任务状态**） |
| Script/MCP 状态 | `ControlState.method`（UNREAL_MCP/UE_PYTHON/...）→ `activity.tool` |
| Pause/Stop 路径 | `ControlCommand(NONE/PAUSE/RESUME/STOP/EMERGENCY_STOP)`，SessionController 语义护栏（不得清掉 PAUSE/STOP/ESTOP） |
| 全局急停 | `EmergencyHotkey`（RegisterHotKey，回退 GetAsyncKeyState 轮询），默认 **ctrl+alt+f12**；`release_all_keys_and_buttons` 记账式只释放 UHA 自己按下的键 |
| 输入释放机制 | `SafetyController`（fail-closed 门 + 租约 + 心跳 + HUMAN_OVERRIDE 终态）+ `human_override.HumanOverrideDetector`（物理输入即夺回） |
| HUD 崩溃时 | 安全层不依赖 HUD ✅（Banner 独立进程内线程、热键在 UHA 进程注册） |
| UHA 崩溃时 HUD | SSE EOF → HUD 显示"离线 Ns"，保留最后状态，自动重连 ✅ |
| HUD 重启恢复 | `GET /stream` 先全量快照再增量 ✅（不会误显示 IDLE） |

### 1.2 已验证的优点（必须保留）

1. **单一状态真源**：`Status` 只定义在 `uah/core/models.py`；映射表唯一存在于 adapter；有测试守漂移。
2. **烂数据不崩 UI**：`StateStore.apply()` 永不抛异常；去重、乱序、协议不兼容全部如实标记。
3. **非阻塞适配器**：出站队列 + 独立发送线程，HUD 卡顿不可能拖住 Executor。
4. **安全层独立**：P0 SafetyController fail-closed、终态只有显式用户动作能离开、急停不依赖 HUD 焦点/鼠标。
5. **"计划=任务"语义修正**：单步 DONE 不再假报任务完成（防弹窗轰炸）。
6. **零第三方依赖**：stdlib only，3.12/3.13 双兼容。

### 1.3 与 v2 需求的差距（本轮要补的）

| # | 差距 | 需求条目 |
|---|---|---|
| G1 | **无 Attention Level**。State 与"用户需不需要关注"混在 Status 里（如 BLOCKED vs ERROR 靠猜） | §六 |
| G2 | **事件类型不够**：无 pause/resume/stop/emergency_stop/input_control_acquired/released/verification_* 事件；HUD 无法区分"执行成功"与"验证通过" | §十二、§十三 |
| G3 | **无 Event Timeline**：Expanded 只有当前 Activity 一行，没有"最近活动"时间线；无法回答"它到底做过什么" | §十一 |
| G4 | **安全状态对 HUD 不可见**：SafetyController 状态（含急停、输入未释放）不进 UAH 事件流；HUD 不显示急停热键提示。现有 SafetyBanner 独立存在但视觉上与 UAH 无关 | §十四、需求图03 |
| G5 | **UI 形态不齐**：现有 Compact/Expanded/Dashboard(Debug)。无 Mini（最小化仅状态）；无独立 Alert 横幅（现在是边框变色+闪一下，层级不够） | §七 |
| G6 | **视觉与概念图不符**：现在是 GitHub Dark 风格，概念图是深蓝黑+冷蓝强调+圆角+OC 印章；OC 完全缺失 | §八、§九 |
| G7 | **无 OC Stamp**：无角色印章槽位与主题层 | §九 |
| G8 | **Verification 状态不可见**：executor 有完整 VERIFYING 阶段与 verification 报告，但 adapter 把 VERIFYING 折叠成 RUNNING，UI 永远显示不出"验证中/验证失败" | §十三 |
| G9 | **Compact 布局简陋**：单行文本 + 细节多行，没有概念图的"状态灯+UAH+OC+状态行+活动行+时间+三按钮"结构；无暂停/停止按钮（暂停只能靠 UHA CLI） | 需求图02 |
| G10 | 技术债：`uah/tests/live_scratch.py`（草稿）、`perf_probe.py`（试验）、`text_hud.py`（保留为降级）、`hud.py` 旧 Dashboard（保留为 Debug 模式，符合 1.1 决策）；`compact.py` 审批轮询放在 `_tick` 里每秒 HTTP（应事件化或降频） | §十七 |

### 1.4 热键冲突说明（需要用户知悉，不擅自改）

- 需求文本与概念图写 **Ctrl+Alt+Esc**；仓库现行约定是 **Ctrl+Alt+F12**
  （`config: desktop.emergency_hotkey`，`SafetySnapshot.hotkey`）。
- 决策：**保留现行 Ctrl+Alt+F12**（§十四"已有全局急停机制优先保留"），
  UAH Alert 显示**真实配置的热键**（从 SafetySnapshot 读取），绝不硬编码文案。
- 如需切换到 Ctrl+Alt+Esc，只改 config 一处，HUD 提示自动跟随。

---

## 2. 风险

| 风险 | 等级 | 对策 |
|---|---|---|
| 工作区有并行会话未提交改动，直接 `git add -A` 会把别人的工作混进提交 | 高 | 只按明确文件路径 add；提交前 `git status` 逐项核对 |
| `.venv`（3.13）无 tkinter，GUI 只能系统 3.12 跑 | 中 | 沿用 launch.py 自动挑解释器；GUI 验收全部用 3.12 |
| 概念图的"玻璃拟态/模糊"tkinter 做不到 | 低 | 用深蓝黑实色 + 细边框 + 圆角矩形（Canvas 画）近似；不做高成本模糊 |
| SafetyBanner 与新 Alert 横幅职责重叠 | 中 | Alert 横幅只做**安全状态可视化**（显示热键、状态、入口），不做急停实现；两者并存，Alert 不抢 Banner 的 fail-closed 职责 |
| 给 SafetyController 加观察者可能碰 P0 安全层 | 中 | 只做**纯增量**：新增 `on_change` 观察者列表与 `add_observer()`，不改任何现有方法语义；HUD 侧只读 |
| OC 无美术素材 | 低 | 实现 `oc_stamp` 资源槽 + 中性占位（不重画角色）；素材后续放入 `uah/theme/assets/` 即生效 |

---

## 3. 建议保留

- `uah/core/*` 全部（models/state/transport/protocol/events/approval/daemon）—— 状态机与传输层是 v2 的地基，**零重写**，只做协议次版本增量。
- `uah/adapters/uha/adapter.py` —— 保留映射表与非阻塞队列，增量加 verification/attention/safety 事件。
- `uah/ui/notify.py`、`ui/components/card.py` —— 保留，扩展。
- `uah/hosts/desktop/launch.py`、`text_hud.py`（降级通道）、`hud.py` Dashboard（Debug 模式）。
- 全部 P0 安全层（`src/safety/**`、`src/desktop/hotkey.py`）—— 一行语义不改。
- `src/core/session_control.py`、`src/scheduler/executor.py` 语义。

## 4. 建议废弃 / 清理（v2 范围内，删除前逐项确认无引用）

- `uah/tests/live_scratch.py`（临场草稿，无测试价值）→ 删除。
- `uah/tests/perf_probe.py` → 保留但移入 `tools/` 性质（暂不删，标记为试验工具）。
- `compact.py` 中每秒一次的审批 HTTP 轮询 → 改为审批事件驱动（Hub approval broker 有 pending 状态可推）+ 至少 2s 降频兜底。

## 5. 计划修改（Phase B→F 落点）

| 阶段 | 修改 |
|---|---|
| B | `uah/core/protocol.py`：次版本 → `uah/1.1`，新增事件类型（pause/resume/stop/emergency_stop/input_control_*/verification_*/safety.*）；`uah/core/models.py`：新增 `AttentionLevel`（L0-L5）+ `AgentSnapshot.attention`；`uah/adapters/uha/adapter.py`：从 VERIFYING 阶段发 verification 事件；**新增** `uah/safety/bridge.py`（订阅 SafetyController.on_change → safety 事件）；`src/safety/controller.py`：纯增量加观察者列表 |
| C | **新增** `uah/theme/tokens.py`（深蓝黑视觉令牌）、`uah/theme/assets/oc_stamp.png`（资源槽+占位）；重构 `uah/hosts/desktop/compact.py` → 四形态：Mini(~190px)/Compact(360×64 级)/Expanded/Alert 横幅；概念图为视觉 Source of Truth |
| D | adapter 发 `execution.mode`（Computer Use/Script/MCP 展示态）；verification.started/passed/failed 进时间线；HUD 不再可能出现"工具成功=完成" |
| E | Hub 内存化 `event ring buffer`（新增 `GET /timeline`，可选）；Expanded 最近活动 3-5 条来自真实事件流；HUD 重连恢复沿用现有全量快照机制并补时间线恢复 |
| F | Compact 加 暂停/停止 按钮（走 SessionController 既有 command 路径，经 Hub 控制端点）；Alert 显示真实热键；HUD 崩溃/重启矩阵测试 |

## 6. 明确不做

- 不重写 UHA Runtime；不建第二套 Agent 状态枚举；不改 `TaskPhase`。
- 不做高成本模糊/Shader/持续动画；空闲 CPU ≈ 0。
- 不动 Release、不 push 远端、不覆盖 `v0.1.0-rc.1`。
