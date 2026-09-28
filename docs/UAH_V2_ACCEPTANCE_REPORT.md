# UAH v2 验收报告（UAH_V2_ACCEPTANCE_REPORT）

> 全部结论来自**本机真实运行**，命令与证据文件见每行「证据」列。
> 判定口径：`PASS` 有真实断言；`FAIL` 跑了但不达标；`BLOCKED` 环境阻断；
> `NOT TESTED` 没跑（**不写"理论支持"/"应该正常"**）。
> 采集时间：2026-09-22，分支 `feature/uah-v2`。

## 0. 测试套件总览（本次实际运行结果）

| 套件 | 结果 | 证据文件 |
|---|---|---|
| `uah/tests/test_uah_phase1.py`（单元+验收自查） | **152 / 152 全部通过** | 含 UHA 回归 388 项一并通过（offline 248 / router 25 / phase2 85 / phase3 11 / phase4 13 / phase4b 6） |
| `uah/tests/v2_integration.py`（真 Executor/Controller/Hub/Safety） | **44 / 44 全部通过** | `artifacts/uah_v2_integration.log` |
| `uah/tests/v2_hud_smoke.py`（真 Hub+SSE+窗口+截图） | **25 / 25 全部通过** | `artifacts/uah_v2_hud_smoke.log`、截图 `artifacts/uah_v2_hud/*.png` |
| `uah/tests/compact_smoke.py`（真实窗口交互） | **21 / 21 全部通过** | `artifacts/uah_v2_gui_test.log` |
| `uah/tests/v2_safety_hotkey.py`（真实全局热键） | **7 / 7 全部通过** | `artifacts/uah_v2_hotkey.log` |
| `uah/tests/gui_smoke.py`（Debug 宿主） | **19 / 19 全部通过** | `artifacts/uah_gui_smoke.log` |
| `uah/tests/perf_probe.py`（性能探针） | 通过（render_card 0.004 ms/次） | `artifacts/uah_perf_probe.log` |
| `uah/tests/test_phase11.py`（.venv 运行） | 16 tests OK | `artifacts/uah_test_phase11.log` |
| `uah/tests/guarded_input_smoke.py`（系统 3.12 运行） | 全 PASS | `artifacts/uah_guarded_input.log` |

---

## 1. 验收矩阵（需求 §十九 的 25 项）

| # | 验收项 | 判定 | 证据 |
|---|---|---|---|
| 1 | UAH 启动成功 | **PASS** | smoke「HUD 启动成功」；compact_smoke 真实建窗口（551×108 @150% DPI） |
| 2 | UHA 未运行任务时正确显示 IDLE | **PASS** | smoke：状态文案 `待机中`，Attention L0 |
| 3 | 任务开始自动变 RUNNING | **PASS** | smoke：事件→`运行中 · Unreal MCP`；integration：真实 `Executor.run()` → RUNNING |
| 4 | 思考状态不无意义打扰 | **PASS** | 设计：THINKING 并入 RUNNING（审查 §5 决策），Attention=L0 无提醒、无横幅；integration 断言 ANALYZING→RUNNING 且 attention 未升高 |
| 5 | Computer Use 启动时 Expanded 正确显示 | **PASS** | integration：`set_method("MOUSE") + set_phase(COMPUTER_CONTROL)` → Tool=`Mouse`，Status 仍 `RUNNING`（不泄露内部阶段） |
| 6 | 切到 Script：UI 自动更新 | **PASS** | integration：`UE_PYTHON → UE Python`、`UE_COMMANDLET → UE Commandlet` |
| 7 | 调用 MCP：UI 自动更新 | **PASS** | integration：`UNREAL_MCP → Unreal MCP`；smoke 同 |
| 8 | 正常 Pause：实际暂停（不是按钮变字） | **PASS（含既有边界，见备注）** | integration：HUD→Hub `/control`→poller→Controller，`blocks_writes=True`、phase=PAUSED、`ensure_writable` 抛 `PreconditionFailed`；暂停期间发起的真实 `Executor.run()` 被拦下（`任务在暂停期间被停止`） |
| 9 | Resume：实际恢复 | **PASS** | integration：`command→NONE`、`blocks_writes=False`、恢复后真实运行跑通；时间线含 `task.resumed` |
| 10 | Stop：真实终止当前任务 | **PASS** | integration：`should_abort=True`；后续真实运行返回 `任务已中止（STOP）`，且 `perform` 调用数为 0 |
| 11 | 鼠标被接管时用户无需鼠标即可急停 | **PASS** | `v2_safety_hotkey.py`：真实注入 ctrl+alt+f12 → 回调触发（键盘路径，未用鼠标） |
| 12 | 热键实际触发安全停止 | **PASS（热键为 ctrl+alt+f12，非 esc）** | 同上 7/7；本机 `RegisterHotKey` 在子线程被拒 → 自动落到轮询回退路径并被真实触发。**未改用 Ctrl+Alt+Esc**：§十四要求"已有全局急停机制优先保留"，现行为配置项 `desktop.emergency_hotkey`，改一处即生效 |
| 13 | 输入没有释放时出现 L5 高优先级 Alert | **PASS** | integration：安全条目 `RUNNING + Attention L5`；smoke：Alert 横幅可见（截图 `06_alert_l5.png`，文案「输入控制未释放」+ 真实热键） |
| 14 | HUD 崩溃：急停仍有效 | **PASS** | integration：`bridge.stop()` 后 `safety.emergency_stop()` 仍 `EMERGENCY_STOP`；急停实现不在 HUD 内（HUD 无急停按钮，`/control` 拒绝 `emergency_stop`） |
| 15 | HUD 重启：恢复当前 State 而不是显示 IDLE | **PASS** | smoke：新建第二个 HUD 实例 → `运行中 · Unreal MCP`；compact_smoke：形态/位置持久化恢复 |
| 16 | UHA 崩溃：HUD 正确显示断连/Error | **PASS（断连路径）** | smoke：Hub 停止 → HUD 存活且 `stream_status=disconnected`，卡片显示离线；**未单独测**"UHA 进程被 kill 后 HUD 表现"（同一 SSE 断连路径，但未做真进程 kill 实验）→ 该子项标注 **NOT TESTED** |
| 17 | Action 成功但 Verification 失败：不允许显示完成 | **PASS** | integration：`verify_passes=False` → Executor ok=False、UAH 状态 `ERROR`（非 DONE）、时间线有 `verification.failed`、**无** `task.completed`、Attention L4 |
| 18 | Verification Passed 才能进入正常完成 | **PASS** | integration：verify 通过 → `verification.passed` + `task.completed` + DONE + Attention L1 |
| 19 | WAITING_USER：Attention 至少达到提醒级别 | **PASS** | smoke：等待批准 → `L3 等待确认（原因）`；unit 断言 approval 事件映射 |
| 20 | Expanded 最近活动与真实执行历史一致 | **PASS** | smoke：时间线文本与注入的事件一一对应（截图 `03/04`）；integration：`verification.* / task.completed / resumed` 均来自真实迁移 |
| 21 | Mini / Compact / Expanded 状态同步 | **PASS** | smoke：三形态共用同一 `AgentSnapshot`；切换后状态文案一致（unit 断言两宿主同源渲染） |
| 22 | OC 没有替换状态灯或 UAH 标识 | **PASS** | smoke：`stamps=3` 且状态灯控件独立存在；截图可见 `● UAH [OC]` 三者并存；缺素材时用中性「OC」占位，未重画角色 |
| 23 | 所有主 UI 中文 | **PASS** | 截图 `01–09`：状态/按钮/详情/Alert 全中文（字体 Microsoft YaHei UI） |
| 24 | 无明显布局错位、文字裁剪、DPI 异常 | **PASS（本机 150% DPI）** | 尺寸断言按 `scale` 计算通过；截图核对无裁剪；已修安全行换行宽度 |
| 25 | 多显示器 / DPI 环境尽可能正常 | **部分 PASS / 部分 NOT TESTED** | DPI：PASS（Shcore 感知 + 尺寸缩放 + 字体自适应，150% 实测）；**多显示器：NOT TESTED**（本机单屏，未做副屏/负坐标实验，代码按主屏夹取） |

### 备注（如实记录，不算 PASS 宣传）

* **#8 的边界**：暂停能可靠拦住"暂停期间发起/继续的写与键鼠操作"；若某次运行只剩一次尝试且
  已在 `perform` 中，暂停来不及在这条运行内生效（UHA 既有语义：`wait_if_paused` 宽限 1s，
  边界检查发生在方法尝试边界）。**本轮未改核心语义**（属 UHA 逻辑，且改动风险高）。
* **#12 的差异**：现行热键是 **Ctrl+Alt+F12**（`desktop.emergency_hotkey`），而非需求文本/概念图里的
  Ctrl+Alt+Esc。按 §十四"已有机制优先保留"未擅自更改；Alert 显示的是**真实配置值**。
  改 Ctrl+Alt+Esc 只需改 config 一处，HUD 文案自动跟随（`parse_hotkey` 已支持 esc）。
* **#13 的触发口径**：Agent 取得键鼠注入许可（`CONTROL_ACQUIRED`/`ACTIVE`）即 L5 + Alert；
  这是概念图 03 的语义（"输入控制已接管 → 提示急停热键"），不是"注入失败"才报。

## 2. 生命周期与异常测试（需求 §十五）

| 场景 | 判定 | 证据 |
|---|---|---|
| HUD 崩溃 / 被手动关闭 | **PASS** | compact_smoke：关闭后 SSE 线程退出、窗口销毁、定时器无泄漏；安全层不受影响（#14） |
| HUD 重启 | **PASS** | smoke：新实例恢复真实状态；形态/位置持久化 |
| UHA 崩溃（进程级 kill） | **NOT TESTED** | 只测了 Hub/SSE 断连路径（HUD 显示离线、保留最后状态、自动重连） |
| Event Stream 断开 | **PASS** | smoke：Hub 停止 → HUD 存活；Hub 同端口重启 → 自动重连并恢复 RUNNING |
| Computer Use 异常 | **PASS（安全侧）** | integration：真实 `SafetyController` 急停 → L5 + ERROR；P0 的 CU 失败路径见既有 P0 报告 |
| Input Release 失败 | **NOT TESTED** | 属 P0 输入层（`tools/` 下有专项脚本，未在本轮重跑） |
| Agent 长时间无响应 | **PASS（机制）** | `StateStore` 心跳过期 → `stale` → HUD 显示「离线 Ns」（unit 有断言）；未做 20s+ 实机挂起实验 |

## 3. 性能（需求 §十六）

| 指标 | 实测 | 说明 |
|---|---|---|
| `render_card()` 单次 | **0.004 ms** | `artifacts/uah_perf_probe.log` |
| 空闲网络活动 | **0** | 事件驱动 SSE；仅 Expanded 2s 拉时间线、WAITING_* 时 2s 拉审批 |
| 空闲 UI 刷新 | 1s 一次 Label 文本更新 | 不重建控件（指纹比较） |
| 动画 | 仅"不确定活动条"，120ms/步，且**窗口不可见或非运行态时不动** | 无持续动画、无模糊、无 Shader |

## 4. 禁止事项自查（需求 §二十）

| 禁止项 | 结论 |
|---|---|
| 为视觉重写整个 UHA | 未发生（executor/router/skills/adapters 业务逻辑未动） |
| 创建第二套 Agent Runtime | 未发生（单一 `StateStore` + 单一 `SessionController`） |
| UAH 决定核心 Agent Logic | 未发生（UAH 只读 + 只请求软控制） |
| UI 按字符串猜状态 | 未发生（全部来自事件；时间线文本由事件字段拼装） |
| 假进度 | 未发生（无 step/total 时用不确定活动条，不显示百分比） |
| 假成功 | 未发生（verify 失败路径已断言无 `task.completed`） |
| 只截图不运行 | 未发生（每个截图都伴随断言；截图是佐证不是证据） |
| 只写 Mock 声称真实可用 | 未发生（集成测试里只有 skill/router 是桩，状态机/执行循环/Hub/安全层是真的，且明确标注） |
| 删掉安全机制后自造更弱的 | 未发生（P0 安全层语义未改；新增的只是只读观察者） |
| OC 喧宾夺主 / 做成桌宠 | 未发生（20px 印章；无情绪符号、无动画） |
| UI 做成复杂 Dashboard | 未发生（四形态，Mini 196×40 常驻；旧 Dashboard 降级为显式 Debug 入口） |
| 未经确认 push GitHub / 发布 Release | 未发生（仅本地 `feature/uah-v2` 提交） |

## 5. 结论

* 25 项验收：**21 项 PASS**，**1 项部分 PASS（多显示器未测）**，**1 项含既有边界的 PASS（暂停）**，
  **1 项 PASS 但热键值与需求文本不一致（已说明）**，**1 项子项 NOT TESTED（UHA 进程级 kill）**，
  另有 2 项生命周期场景 NOT TESTED（Input Release 失败专项、长时间无响应实机）。
* 未发现任何"报告绿了、实际没修好"的形态：所有 UI 文案都能追到真实事件；
  verify 失败时 UI 不可能显示完成。
* 后续可交付项：① 放入 OC 官方印章素材；② 决定急停热键是否改为 Ctrl+Alt+Esc；
  ③ 是否合并 Alert 与 SafetyBanner；④ 多显示器专项。
