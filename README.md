# AI进度插件

UAH（Universal Agent HUD）：为 AI / Agent 任务提供桌面状态条、真实步骤进度、异常提示和软控制。

## 当前能做什么

- Mini / Compact / Expanded 桌面形态和异常提示。
- 显示任务、步骤、执行方式、耗时；按验证完成的步骤计算进度。
- 未知总量显示活动状态，不伪造百分比。
- 暂停、继续、停止请求及执行方回执。
- 心跳失联检测、断线重连、多 Agent 选择。
- 已接入随仓库提供的 UHA 执行器；外部程序可以通过通用 Python 桥、HTTP 或命令行上报事件。

## AI 接入边界

插件是仪表盘，本身不调用大模型、不决定任务计划，也不会自动读取其他 AI 软件的内部状态。当前 UHA 规划器使用规则解析，尚未提供开箱即用的模型 API 配置与自然语言自主规划。外部 AI 程序需要接入事件上报，软控制也需要执行方配合。

## Windows 快速开始

需要 Python 3.10+ 和 tkinter。启动器支持通过 `UAH_PYTHON` 指定 Python 路径。

第一个终端启动 HUD：

```powershell
.\start_uah_hud.cmd --mode compact
```

第二个终端启动演示（不调用模型，不操作 UE）：

```powershell
.\demo_uah_progress.cmd --scenario all
```

演示依次运行成功、失败、未知总量、暂停。最后在 HUD 点击继续或停止。

## 自动测试

```powershell
.\test_uah_progress.cmd
```

预期 `PASS: 8/8 suites`。日志位于 `artifacts/uah-progress/`。
本次验证包含隐藏 Tk 控件的四档缩放测试；真实 UE 操作、物理急停、多屏拖动和插拔仍需实机验收。历史文档提到的独立安全/审批模块未包含在当前基线中，不能视为已接入。

## 文档与目录

- [完善内容与完整测试方法](docs/UAH_PROGRESS_COMPLETION.md)
- [UHA 执行器说明](docs/UHA_README.md)
- `uah/`：仪表盘、状态 Hub、通用适配器与界面。
- `src/` 和 `uha.py`：UHA 执行器及现有 UE 技能，保留用于实际接入和回归测试。
- `config/agent.config.example.json`：配置示例；本机配置和密钥不提交。

旧交接与架构文档保留供参考，其中的旧测试数量、机器路径和安全模块描述不代表当前验收结果。

## 许可

参见 [LICENSE](LICENSE)。

## Windows 双击使用说明

- `start_uah_hud.cmd`：启动窗口。展开窗口右上角 × 关闭 HUD，不再缩回小条。
- `demo_uah_progress.cmd`：先启动 HUD 再双击；默认演示约 30 秒，结束后保留结果窗口。若看不到进度，右键 HUD → 当前 Agent → 选择 UAH 功能演示。
- `test_uah_progress.cmd`：后台自动验收，不改变已经打开的 HUD；无参数运行结束后按任意键关闭结果窗口。
- `run_uah.cmd`：其他脚本共用的内部启动器；直接双击只显示使用提示。

演示是预设测试事件，不代表插件已读取真实 AI 的思考状态。关闭 HUD 不会停止独立运行的 Agent 或共享状态 Hub。
