# UAH 进度条完善与测试说明

日期：2026-09-28。

本次基线是本机 UnrealHybridAgent 的 `fb34270`（phase3）以及 2026-09-22 的 UAH 交接快照。当前仓库没有交接文档中的 `05d8a31`、`src/safety` 和 `src/core/execution_mode.py`，因此本次补上现有 Executor/CLI 的真实接入，保留可选安全桥和审批接口，不把不存在的模块显示为已经启用。历史 `UAH_V2_*` 报告属于旧环境，本次验收以本文件和新测试日志为准。

## 已实现

| 项目 | 当前行为 |
|---|---|
| 默认进度条 | Compact 和 Expanded 都使用真实的已验证步骤数；未知总量显示活动动画，不编百分比 |
| 完成口径 | `step` 表示当前步骤，`completed_steps` 表示验证通过的步骤。最后一步执行/验证期间不能出现 100%；整个任务验证完成才显示 100% |
| 任务切换 | 每次原生计划或单任务生成新 ID、独立开始/结束时间。新任务不继承旧进度；协议显式 null 可以清空字段 |
| 重试与失败 | 重试不会重复计数。失败、取消不显示完成；预演、缺少验证证据、可选步骤验证失败显示 WARNING |
| 验证事件 | 接入新版 Executor 时，`verification.passed` 来自真实验证结果，不根据单纯 DONE 阶段推断 |
| 离线与重连 | 离线停止活动动画；Hub 无新事件时也会推送心跳过期状态。重连/Hub 重启后由完整心跳恢复任务、进度和配置 |
| 暂停 | 执行器在安全执行边界等待继续/停止，不再因暂停超过 1 秒而失败。已提交给后端的操作可能仍需收尾 |
| 控制反馈 | 按当前 Agent 定向提交；异步发送不阻塞 Tk；显示请求中、执行方回执、拒绝、失败、超时。过期命令不再执行，重复回执拒绝 |
| 控制安全语义 | 仍只接受 pause/resume/stop；拒绝 emergency_stop；暂停/恢复不得覆盖 STOP/EMERGENCY_STOP |
| 时间 | 使用任务的开始/结束时间。任务完成后冻结；换任务归零；未知起点显示“—” |
| 长时间无进度 | 保持在线但 60 秒没有任务进度变化时，显示“无进度更新（仍在线）”，不自行判失败或编 ETA |
| 多屏 | 通过 Win32 枚举工作区，支持主屏左侧/上方负坐标、工作区夹取、显示器移除后找回窗口，以及跨 DPI 重建控件 |
| 字体与图标 | 按可用字体回退，按 DPI 设置文字尺寸。操作按钮和状态灯使用 Tk 位图绘制，不依赖特殊符号字体 |
| 设置页 | 从 Agent 上报的白名单读取任务域、预演状态、权限配置、审批接入和实际热键启用状态；配置只读，不放无效开关，不传密钥 |
| 告警 | L4 引导查看任务/验证，L5 单独显示急停信息；没有普通任务时安全告警仍显示；L5 不被之前的临时关闭压住；Mini 告警临时加宽避免裁切 |
| 多 Agent | 右键“当前 Agent”可选具体任务来源或自动选择；安全遥测不被当成可暂停的执行任务 |
| 事件完整性 | 修复同一时钟刻度内进度更新被 SSE 去重吞掉的问题；flush 等待发送完成，而不是只检查队列是否为空 |

外部 Agent 若只发送旧的 `step/total_steps`，仍显示“当前步骤”，但不会将其冒充完成百分比。要显示确定比例，请增加 `completed_steps`。推荐同时传 `task.id` 和 `started_at/ended_at`。

## 一键自动测试

在 PowerShell 中执行：

```powershell
cd E:\UnrealHybridAgent
.\test_uah_progress.cmd
```

预期最后输出 `PASS: 8/8 suites`，退出码为 0。日志和 `summary.json` 写到 `artifacts/uah-progress/日期时间/`，失败项会显示 FAIL 和错误尾部。

入口优先使用 `UAH_PYTHON` 指定的解释器，其次探测本机 Codex 附带 Python、`py -3`、PATH 中可运行的 Python。可显式指定：

```powershell
$env:UAH_PYTHON = 'C:\你的Python目录\python.exe'
.\test_uah_progress.cmd
```

需要 Python 3.10+；桌面和隐藏控件测试需要 tkinter。只测试无界面环境时可以用 `test_uah_progress.cmd --no-widgets`，此时预期 7/7，日志会明确标记跳过控件测试，不能当成界面验收通过。

| 套件 | 覆盖 |
|---|---|
| syntax | 新旧 Python 文件编译检查 |
| hub-selftest | 11 项真实 Hub/协议/发送与读取自检 |
| progress | 37 个专项测试：真实 Executor、验证失败及重试、暂停超过一秒、停止解除暂停、任务切换、时间、定向 HTTP 控制、回执与过期、真实子进程强杀、持续 SSE 离线通知、Hub 重启重连、CLI 装配、队列 flush 等 |
| legacy-hud | 131 项适用于当前基线的既有 HUD/通用适配器/状态流断言；完成量测试按新协议补入真实验证计数 |
| uha-offline | 245 项现有离线执行回归 |
| uha-router | 24 项现有路由回归 |
| uha-controls | 11 项现有控制状态回归 |
| hidden-widgets | 真实 Tk 控件在隐藏窗口中验证：100%/125%/150%/200%，动态切换到 175%，各形态和页签、进度比例、回执展示、图标、设置和告警 |

自动测试不打开 UE、不调用 Computer Use、不注入键鼠、不抓取桌面。进程强杀只终止测试自行创建的子进程。隐藏控件测试不会显示窗口、抢焦点或控制其他应用。

旧 `v2_integration`/`test_phase11` 依赖交接包未提供的独立安全/审批模块；旧急停冒烟会真实注入按键。本次一键测试不运行它们。它们仍作为历史材料保留，不能把旧报告里的通过数量累计到本次结果。

## 手动看进度条

先启动 HUD，再运行演示；演示不需要 UE：

```powershell
cd E:\UnrealHybridAgent
.\start_uah_hud.cmd --mode compact
.\demo_uah_progress.cmd --scenario success
```

演示 Agent 名称是“UAH 功能演示”，ID 为 `uah-demo-进程号`。如果当前显示其他 Agent，右键 HUD → 当前 Agent → 选择对应演示。演示退出后保留最后状态并标为离线是正常的。

| 操作 | 应看到什么 |
|---|---|
| `demo_uah_progress.cmd --scenario success` | 三步计划：验证完成量按 0→1→2→3 增加；最后验证前不出现 100%，结束才变绿并到 100% |
| `demo_uah_progress.cmd --scenario failure` | 第二步验证失败，显示失败；完成量停在 1/3，不出现完成或 100% |
| `demo_uah_progress.cmd --scenario unknown` | 新任务总量未知，只显示活动动画，不残留上次 1/3 或 3/3；结束停止动画 |
| `demo_uah_progress.cmd --scenario pause` | 第二步自动暂停。在 HUD 点“继续”后继续；等待超过 1 秒也不会报失败 |
| 暂停场景中点“停止” | 显示请求和回执，任务结束为已停止；不显示成功 |
| `demo_uah_progress.cmd --scenario all` | 依次跑成功、失败、未知总量、暂停；最后需要在 HUD 点继续或停止 |

需要更多操作时间：加 `--step-seconds 10`。演示的“验证”是明确标记的测试流程，只验证 HUD 行为，不代表真实 UE 内容验证。

## 断线、重连和多屏验收

1. 运行 `demo_uah_progress.cmd --scenario success --step-seconds 60`。终端会打印演示的 Agent ID，其中数字是演示进程 PID。
2. 如需检查真实失联路径，只结束这个演示进程：`Stop-Process -Id <演示PID> -Force`。不要结束真实工作中的 UHA/UE。默认从最后一条 Agent 事件算起约 20–21 秒后，HUD 应显示离线并停止活动动画。自动专项测试使用较短阈值验证相同路径。
3. 重新启动演示或重启 HUD，应恢复当前快照而非从旧进度继续。自动套件还会独立重启临时 Hub，验证原有 SSE 客户端自动重连。
4. 手动把 HUD 拖到主屏左侧/上方的副屏，在不同缩放屏幕间移动；检查文字、进度条、告警完整显示。关闭后重开应恢复可见位置。
5. HUD 留在副屏后断开副屏，约 2 秒内应被夹回现有工作区；任务栏占用区域不应遮住控件。
6. 展开“设置”，核对显示值与实际 Agent 配置一致。无审批/独立安全模块时应显示未接入；未启用热键时不能显示“已启用”。

## 验收边界

- 多屏坐标算法、Win32 工作区枚举、负坐标 Tk 几何语法和动态 DPI 控件重建已有自动测试；真实多显示器拖动/插拔仍需按上面的步骤在实际硬件验收。
- 当前基线的独立安全/审批源代码不在交接包中。HUD 只显示实际接入情况，未补造安全执行能力。
- 本次没有进行真实 UE 场景写入、物理急停按键或输入释放实验。不要用演示任务或隐藏控件测试替代这些验收。
- Tk 界面仍使用深色半透明效果，没有引入新 GUI 框架或实现系统级背景模糊。

## 关键文件

- `src/scheduler/executor.py`：公开进度观察接口、步骤结束后的验证计数、暂停等待。
- `uah/adapters/uha/adapter.py`：任务 ID/生命周期/完整心跳、实际验证结果、可靠 flush。
- `uah/core/models.py`、`state.py`、`transport.py`：协议 1.2、明确清空、SSE 变化签名、过期推送、控制回执。
- `uah/hosts/desktop/compact.py`：进度条、异步软控制、设置、形态和告警。
- `uah/ui/progress.py`、`monitors.py`、`fonts.py`、`icons.py`、`runtime_settings.py`：可单测的展示与配置模块。
- `uah/tests/verify_progress.py`：推荐的一键验收入口。

本次改动只在本地交付，没有推送远程仓库或发布 Release。
