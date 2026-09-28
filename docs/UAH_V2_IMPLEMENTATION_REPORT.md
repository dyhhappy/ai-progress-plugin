# UAH v2 实现报告（UAH_V2_IMPLEMENTATION_REPORT）

> 分支：`feature/uah-v2`（基线 `0108a93`，未 push、未动 Release、未覆盖 tag `v0.1.0-rc.1`）
> 时间：2026-09-22　验收结果见 `docs/UAH_V2_ACCEPTANCE_REPORT.md`
> 审查报告见 `docs/UAH_V2_AUDIT.md`

---

## 1. 文件清单

### 新增

| 文件 | 作用 |
|---|---|
| `uah/core/attention.py` | Attention L0–L5 定义、Status→Attention 默认推导、解析（延迟导入避免循环依赖） |
| `uah/core/control_poller.py` | Hub 控制队列 → `SessionController.pause/resume/stop`（长轮询，拒绝一切非软控制动作） |
| `uah/safety/__init__.py`、`uah/safety/bridge.py` | 安全层只读订阅 + 安全事件条目（`uha-safety`），L5 判定与热键透传 |
| `uah/theme/__init__.py`、`uah/theme/tokens.py` | 视觉令牌：深蓝黑配色、中文状态文案、Attention 颜色、形态尺寸、字体 |
| `uah/theme/assets/README.md` | OC 印章资源槽说明（放 `oc_stamp.png` 即生效；不重画角色） |
| `uah/ui/oc.py` | OC 印章组件（官方素材优先，缺素材用中性「OC」占位章） |
| `uah/tests/v2_hud_smoke.py` | HUD 真实运行冒烟（真 Hub+SSE+窗口+PNG 截图）25 项 |
| `uah/tests/v2_integration.py` | 端到端集成（真 Executor/Controller/Hub/Safety）44 项 |
| `uah/tests/v2_safety_hotkey.py` | 全局急停热键真实验证（真注册/真注入/真回调）7 项 |
| `docs/UAH_V2_AUDIT.md` | Phase A 审查报告 |

### 重写

| 文件 | 说明 |
|---|---|
| `uah/hosts/desktop/compact.py` | v1 的 Compact/Dashboard 双形态 → v2 **四形态**（Mini/Compact/Expanded/Alert），全部布局、按钮、时间线、批准行、圆角、DPI |
| `uah/tests/compact_smoke.py` | 从 v1 断言（320×48 / `set_mode` / dashboard）改写为 v2 真实窗口测试 21 项 |

### 修改

| 文件 | 改动 |
|---|---|
| `uah/core/protocol.py` | 版本 `uah/1` → **`uah/1.1`**（只增不删）；新增 12 个事件类型常量 |
| `uah/core/models.py` | `Event.attention/attention_reason`、`AgentSnapshot.attention/attention_reason`；线上往返（显式 L0 用键存在判断） |
| `uah/core/state.py` | Attention 摄入与默认推导、`ApplyOutcome.attention_changed` |
| `uah/core/transport.py` | 每 Agent 事件时间线（环形缓冲 200）+ `GET /timeline`；`POST /control` + `GET /control/pending?wait=`（能力令牌）；`verify_capability`；`HubClient.timeline/submit_control` |
| `uah/core/approval.py` | `control_call` / `fetch_control_commands`（复用同一份本机能力令牌） |
| `uah/adapters/uha/adapter.py` | `task.paused`/`task.resumed` 事件类型；`_emit_phase_transitions()`（从真实阶段迁移推导 verification 事件）；等待/阻塞的显式 Attention |
| `uah/hosts/embedded/bootstrap.py` | 装配 SafetyBridge 与 ControlPoller，收尾时停止它们 |
| `uah/hosts/desktop/hud.py` | `HudApp` 惰性导出（消除 import 环）；Debug 宿主 `build()` 末尾 `deiconify`（否则窗口不显示、geometry 不生效——遗留缺陷） |
| `uah/hosts/desktop/__main__.py` | `--mode mini|compact|expanded|dashboard`（dashboard = 旧调试宿主） |
| `uah/tests/test_uah_phase1.py` | 协议版本断言接受次版本（`uah/1.x`） |
| `uah/tests/gui_smoke.py` | 指向 Debug 宿主；补一次事件循环再量尺寸（原断言在 geometry 生效前取值） |
| `src/safety/controller.py` | **纯增量**：`add_observer` / `remove_observer` / `_notify_observers`（约 35 行）。现有方法语义、状态机、fail-closed 规则一行未改 |
| `.gitignore` | `_*.py` 规则误伤 `uah/**/__init__.py`，加例外 |
| `UAH_ARCHITECTURE.md` | 追加 UAH v2 章节（概念模型/协议/UI/安全/控制/主题） |

### 删除

| 文件 | 理由 |
|---|---|
| `uah/tests/live_scratch.py` | v1 时期的临场草稿，无断言、无引用（审查报告 §4 已列出） |

> 说明：本分支从 `p01-human-override` 的**未提交工作区**开出。工作区里已有的他人/并行会话改动
> （`src/safety/banner.py`、`watchdog.py`、`src/adapters/**`、`uah/ui/notify.py` 等）**原样保留、未被回退**，
> 提交时按文件路径逐项挑选，没有 `git add -A`。

---

## 2. 架构变化

v1 → v2 是**增量**，四条主线：

```
① 概念解耦     Status（机器在干什么）  ⊕  Attention（人要不要管）
② 事件扩容     uah/1 → uah/1.1：验证 / 暂停恢复停止 / 输入接管 / 安全 事件
③ 安全可视     SafetyController ──只读观察者──▶ SafetyBridge ──▶ 独立安全条目 + L5 Alert
④ 交互闭环     HUD 软控制 ──▶ Hub /control ──▶ ControlPoller ──▶ SessionController
```

**没有**改动的东西（刻意）：`src/scheduler/executor.py` 的业务逻辑、`src/core/session_control.py`
的状态机与护栏、`src/router/**`、`src/skills/**`、P0 安全层语义、`ControlOverlay`（保留）。
UAH 依然只是「读者 + 展示者 + 请求者」，不决定任何 Agent 逻辑。

## 3. State 设计

沿用 v1 的 `Status`（唯一定义处 `uah/core/models.py`）+ `TaskState`（phase/stage/step）
+ `Activity`（summary/detail/tool）三层语义，未新增枚举、未引入第二套状态源。

映射表仍只有一张（`uah/adapters/uha/adapter.py`）：

| UHA `TaskPhase` | UAH `Status` | 说明 |
|---|---|---|
| IDLE | IDLE | |
| ANALYZING / STRUCTURED_EXECUTION / COMPUTER_CONTROL / VERIFYING | RUNNING | 内部阶段不泄露到协议 |
| PAUSED | PAUSED | 有独立事件 `task.paused` |
| STOPPING | RUNNING | 还在收尾，不提前报 CANCELLED |
| DONE / FAILED / ABORTED | DONE / ERROR / CANCELLED | |

## 4. Attention 设计

* 取值 `L0 无需关注 / L1 状态变化 / L2 建议查看 / L3 等待确认 / L4 必须处理 / L5 安全紧急`；
* 默认推导表在 `attention.py`（RUNNING=L0，DONE=L1，PAUSED=L2，WAITING_*=L3，ERROR/BLOCKED=L4，
  安全接管与急停=L5）；事件可显式覆盖并带 `attention_reason`；
* 与 Status **正交**：同一 Status 在不同上下文可对应不同 Attention（安全条目 RUNNING 时是 L5，
  任务条目 RUNNING 时是 L0）；
* HUD 排序、Alert 触发只看 Attention，不看颜色表；
* 坑（已修）：`Attention.L0 == 0` 是 falsy，线上往返必须用"键是否存在"判断，否则显式 L0 被默认推导覆盖。

## 5. Event Stream 设计

`uah/1.1` 事件全集（新增部分加粗）：`agent.started|stopped|heartbeat`、`task.started|stage_changed|step_changed|completed|failed|cancelled`、
**`task.paused|resumed|stop_requested`**、`activity.changed`、`approval.required`、`user_input.required`、
**`verification.started|passed|failed`**、**`input.control_acquired|released`**、**`safety.emergency_stop|safety.changed`**、
**`attention.changed`**、`status.changed`。

单一真源不变：`StateStore` 是唯一状态机，Hub 是唯一汇聚点，UI 只读快照。新增
`GET /timeline`（每 Agent 200 条环形缓冲）供「最近活动」使用，文本只由事件自身携带的信息拼出
（事件类型 + activity），**不编造**。

## 6. UI 四种形态

| 形态 | 触发 | 内容 |
|---|---|---|
| Mini | 默认最小化/右键菜单 | 状态灯 + `UAH` + OC 印章 + 展开箭头（196×40 逻辑像素） |
| Compact | 默认形态 | 状态灯/`UAH`/OC ｜ `运行中 · Unreal MCP` ｜ 运行时间；第二行：活动行 + 暂停·展开·停止；底部不确定活动条（368×72） |
| Expanded | 点「展开」或双击 | 上述 + 当前任务 / 当前动作 / 执行方式 / 关注级别 / 安全状态 / 最近活动 5 条 + 批准行（有 pending 时）+ 暂停·停止·收起（368×296） |
| Alert | 任一 Agent Attention ≥ L4 | 独立置顶横幅：L5 红（优先讲安全，如「输入控制未释放」）、L4 橙；显示**真实急停热键**；可关闭（60s 内不重复弹） |

工程细节：`-topmost` + `WS_EX_NOACTIVATE`（永不抢焦点、不进 Alt-Tab）、圆角用透明键色四角掩码 +
内容帧内缩、提醒**不抬窗**、拖拽 + 形态/位置持久化、DPI 感知（Shcore→user32 降级链）、
字体随 DPI 缩放、窗口尺寸 = 逻辑尺寸 × scale。

## 7. OC 集成方式

* 位置：品牌区 `[状态灯] UAH [OC 印章] | 状态`——OC 是**额外**印章；
* 不替换状态灯、不替换 `UAH` 文字、不做桌宠、不使用 !/✨/爱心 等符号；
* 素材：`uah/theme/assets/oc_stamp.png`（可选 6 个状态变体 `oc_stamp_{idle|working|thinking|waiting|warning|completed}.png`）；
  缺素材时渲染中性「OC」占位章并在日志提示一次——**绝不擅自重画角色**；
* 当前仓库**尚未放入**官方印章图（占位生效），这一步需要用户提供素材。

## 8. Safety 架构与急停关系

```
User Input  >  CU Safety  >  Task Completion  >  UI
（优先级不变，来自 P0）

SafetyController（src/safety，语义未改）
   │  add_observer（v2 新增，只读）
   ▼
SafetyBridge（uah/safety/bridge.py）
   │  状态 → 事件（独立条目 uha-safety，Attention L5 优先）
   ▼
Hub ──▶ HUD 安全状态行 + Alert 横幅（只展示 + 只提示热键）
```

* **急停本身仍在 `src/desktop/hotkey.py` + `SafetyController`**，v2 一行未动；
* HUD 里**没有**急停按钮；`POST /control` 明确拒绝 `emergency_stop`；
* 安全层不依赖 HUD/桥存活：桥停掉后急停照常工作（集成测试有断言）；
* Alert 显示的热键来自真实安全状态（`activity.detail` 的 `hotkey=`），不是硬编码文案。

## 9. Verification 状态

* `verification.started`：观察到 `TaskPhase.VERIFYING` 进入；
* `verification.passed`：`VERIFYING → DONE`；
* `verification.failed`：`VERIFYING → 执行阶段`（fallback 换通道）或 `VERIFYING → FAILED`；
* 判定完全由**真实阶段迁移**推导，不猜、不编；
* 结果：`action 成功 + verify 失败` ⇒ UAH 侧是 ERROR + L4 + `verification.failed`，
  **不存在** `task.completed`（假绿防线，集成测试断言）。

## 10. Reconnect 机制

* SSE 断线自动重连（指数退避），重连后服务端先发全量快照 → HUD 以全量重建视图（不是"等下一次变化"）；
* HUD 侧收到 `connected` 时清空本地快照，避免残留旧 Agent；
* 时间线按需重拉（Expanded，2s，后台线程，失败静默）；
* 形态/位置/选中项持久化在 `.state/uah/window.json`，重启恢复；
* 实测：Hub 停→重启（同端口）后 HUD 会自动重连并恢复到真实状态。

## 11. 已清理技术债

| 项 | 处理 |
|---|---|
| `uah/tests/live_scratch.py` | 删除（无引用） |
| Compact 每秒审批 HTTP 轮询 | 改为 2s、且只在"展开态或有 Agent 处于 WAITING_*"时才拉 |
| v1 的 `set_mode()`/内置 approval 行命名混乱 | 形态 API 统一为 `_apply_form(mini/compact/expanded)`；批准行回归（v1 能力未丢） |
| `hud.py` 与 `compact.py` 模块级互相 import | 改为惰性 `__getattr__` 导出，两个方向导入都安全 |
| Debug 宿主 `build()` 不 deiconify（窗口永不显示 / geometry 不生效） | 修复 |
| `.gitignore` 的 `_*.py` 误伤 `uah/**/__init__.py` | 加例外 |
| `Attention` 与 `models` 的循环导入隐患 | 默认推导表延迟构建 |

## 12. 仍存在的问题（如实列出）

0. **急停热键已决定为 Ctrl+Alt+F12**（见 `docs/UAH_UI_V2_REFINEMENT_REPORT.md` §10.1；
   代码默认值、示例配置、热键解析已全部对齐，UAH 提示条显示真实值）。
1. **暂停语义的既有边界**：`SessionController.wait_if_paused` 的宽限期是 1s。
   若运行中只剩一次尝试且 perform 已在执行，暂停来不及在本次运行内生效——
   该次运行会正常跑完。暂停能**可靠拦住的是**"暂停期间发起/继续的写与键鼠操作"
   （`ensure_writable` / `blocks_writes` 硬拦），以及多尝试/多步运行的下一个边界。
   这是 UHA 既有设计，本轮**没有**改（改它属于改核心 Agent 逻辑），已在验收报告如实标注。
2. **OC 美术素材缺失**：当前是占位章；放入 `uah/theme/assets/oc_stamp.png` 即生效。
3. **热键注册在本机常落到轮询回退**：`RegisterHotKey` 在子线程被系统拒绝时自动退回
   50ms 采样轮询；两条路径都可用（真实注入验证通过），但轮询对"瞬时按键"不灵敏
   （按住 ≥0.3s 才稳），且 50ms 采样有极小 CPU 成本。
4. **多显示器**：HUD 位置按主屏尺寸夹取；跨屏拖动/负坐标屏（副屏在主屏左侧）未专门处理。
5. **Alert 与 SafetyBanner 并存**：两者职责不同（前者是 UAH 的高优先级提示层，后者是 P0
   的键鼠安全横幅），视觉上可能出现两条横幅同时存在；是否合并需产品决策。
6. `uah/ui/notify.py`、`hosts/desktop/text_hud.py`、`ui/components/*`（v1 组件）仍在用，
   但风格尚未跟随 v2 令牌（本轮只重写了主 HUD）。

## 13. 验证命令（可复现）

```bash
PY312="C:/Users/16664/AppData/Local/Programs/Python/Python312/python.exe"   # 有 tkinter
$PY312 -u uah/tests/v2_hud_smoke.py        # 25 项，真实窗口 + 截图 → artifacts/uah_v2_hud/
$PY312 -u uah/tests/compact_smoke.py       # 21 项，真实窗口交互
$PY312 -u uah/tests/v2_safety_hotkey.py    #  7 项，真实系统热键 + 真实注入
$PY312 -u uah/tests/gui_smoke.py           # 19 项，Debug 宿主
<python313> uah/tests/v2_integration.py    # 44 项，真实 Executor/Controller/Hub/Safety
<python313> uah/tests/test_uah_phase1.py   # 152 项（含 UHA 388 回归）
```
