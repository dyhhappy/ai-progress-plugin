# 用 UAH 启动命令行 Agent

这是一层通用进程包装器，不依赖 UHA、UE 或模型 API Key。真实 AI 工具仍需自行安装、登录并配置模型。UAH 不会读取已经打开的桌面聊天，也不会获得模型内部思考进度。

## 最简单的用法

1. 双击 `launch_agent.cmd`，选择找到的命令行 Agent。入口会另外打开 HUD。
2. 在 Agent 自己的终端输入需求，像平常一样使用。
3. HUD 显示本次进程状态。若固定选择了旧 Agent，右键 → 当前 Agent → 选择新启动的来源。
4. 已经打开 HUD 时，可右键 → 启动命令行 Agent，避免再开一个 HUD。

启动器会在 PATH 查找 `codex`、`claude`、`gemini`、`opencode`、`aider`。这只代表找到可执行文件，不代表它们的所有版本或交互界面都已实机验证。列表为空时可以配置绝对路径。

## 两种模式

| 模式 | 适合场景 | 看到什么 |
|---|---|---|
| terminal（默认） | 交互式 CLI、终端界面 | 原终端直接连接子程序；HUD 显示进程运行/退出，没有步骤百分比 |
| capture | 输出文本或逐行事件的程序 | 输出仍显示在终端；HUD 显示输出活动，有明确事件时显示等待、步骤、结果 |

capture 使用标准管道，不是 PTY/ConPTY。依赖完整终端控制的工具请使用 terminal。普通文本里的“成功”“100%”不被当作任务完成证据。不会根据 CPU、沉默时间或输出条数推算完成比例。

两种模式均保留标准输入，输入仍在启动器终端完成。**暂停/继续不支持**，HUD 会将暂停标为不可用。停止结束本次进程组/作业，包括其普通子进程；不是温和取消，也不撤销已写入的文件或远端操作。退出父程序时同组残留子进程也会被清理，不适合启动需要长期脱离父进程运行的服务。通过提权、外部服务启动等机制脱离进程组的程序不在管理保证内。

关闭 HUD 仅关闭显示窗口，Agent 和共享 Hub 独立运行。结束任务请在终端退出，或点 HUD 的停止。

## 自定义工具

复制 `config/agents.example.json` 为 `config/agents.local.json`，修改后保存。例如：

```json
{
  "my-agent": {
    "name": "我的 AI 工具",
    "command": ["C:/Tools/agent.exe"],
    "cwd": "E:/my-project",
    "mode": "terminal",
    "share_output": false
  }
}
```

`command` 必须是程序和参数的数组，不是 shell 表达式。相对 `cwd` 按配置文件所在目录解析；未配置工作目录时继承启动器目录。密钥沿用工具自己的登录/环境配置，不要放入命令行参数。此本地配置已被 Git 忽略。

Windows 的 `.cmd/.bat` 支持普通参数，但拒绝引号、换行和 `& | < > ^ % !` 等 shell 特殊字符。如果需要复杂提示词参数，配置 `node.exe` 与 CLI 的 JavaScript 入口，或者在 terminal 模式启动后交互输入。

PowerShell 示例：

```powershell
cd E:\AI进度插件
.\launch_agent.cmd --list
.\launch_agent.cmd --profile my-agent --hud
.\launch_agent.cmd --name "我的脚本" --mode capture -- C:\Python312\python.exe -u E:\my-agent\main.py
```

`--hud` 每次都会新开一个窗口，已有窗口时省略它。非 UTF-8 程序可用 `--encoding gbk`。原始输出默认仅在终端显示，HUD 只显示行数；显式加 `--share-output` 才转发清理/截断后的摘要。脱敏仅覆盖常见格式，不是所有敏感信息的保证。

## 可选结构化事件

capture 模式只识别 stdout 中以 `UAH_EVENT ` 开头的单行 JSON，其他 JSON 或 stderr 不作为协议。接入方只能报告本次任务，不能更换 Agent 身份或控制能力。

```text
UAH_EVENT {"type":"progress","completed":1,"total":3,"message":"完成第一步"}
UAH_EVENT {"type":"waiting_input","message":"请在终端输入"}
UAH_EVENT {"type":"running","message":"继续执行"}
UAH_EVENT {"type":"completed"}
```

支持类型：`activity`、`progress`、`waiting_input`、`waiting_approval`、`running`、`completed`、`failed`。

- 等待审批只显示状态，必须回到原终端处理，UAH 不自动批准。
- 总数必须是正整数，完成数必须是 0 到总数之间的整数。
- 进度标注“已上报”，不是 UHA 的“已验证”。
- completed 后仍等待进程退出；退出码非零会覆盖完成声明并显示失败。
- 仅退出码为零但无 completed：显示“进程正常退出；任务结果未验证”。
- completed 且退出码为零：显示程序自报完成，仍不代表独立验证通过。
- failed 后即使退出码为零，HUD 也显示失败。
- 心跳与完整快照由包装器发送，Hub 短暂断开不阻塞子程序；执行期间重连可恢复状态。任务结束时若三次发送均失败，终端会明确提示最终状态未送达。

## 不用 AI 的真实进程链路测试

这些是明确标记的测试子程序，并非大模型任务。与旧 demo 不同，它们由包装器创建真实进程、接收输出、观察退出并转发事件。

```powershell
.\launch_agent.cmd --sample events --hud
.\launch_agent.cmd --sample plain
.\launch_agent.cmd --sample failure
.\launch_agent.cmd --sample input
```

events：0/3 → 3/3，程序自报完成；plain：无百分比，结束结果未验证；failure：失败，退出码 7；input：显示等待输入，在终端输入后继续。

要测试停止，在自定义程序长时间运行时点 HUD 停止，检查进程结束、状态为已停止，其他独立进程仍在。不要用正在进行重要工作的 Agent 试验强制停止。

完整自动验收：`test_uah_progress.cmd`，预期 **PASS: 9/9 suites**。其中 cli-wrapper 包含真实子进程、HTTP 控制、进程树、输入输出、中文路径和协议校验；并不等于已经验证每家真实 AI CLI 的所有版本。
