# UnrealHybridAgent（UHA）

**UE5 混合自动化运行时** —— 让一个 Agent 在真实 UE 编辑器里干活：自动挑执行方式、失败自动降级、干完自动复验；并且**在它不确定的时候拒绝动手**。

```
                    ┌──────────────┐
   自然语言 / 参数 ──▶│  Planner     │  把目标编成显式、可核对的步骤
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐
                    │  Router      │  规则 + 成本模型 → 选最快最稳的方式
                    └──────┬───────┘   并给出完整降级链
                           ▼
   ┌───────────────────────────────────────────────────────┐
   │  Executor：perform → verify → 不通过就换下一种方式      │
   │            （相对位移这类"不可幂等"动作另有护栏）        │
   └───────┬───────────────────────────┬───────────────────┘
           ▼                           ▼
   ┌───────────────┐          ┌─────────────────┐
   │ 结构化通道     │          │ GUI / 视觉通道   │
   │ Unreal MCP    │          │ MOUSE/KEYBOARD  │
   │ UE Python     │          │ VISION/HYBRID   │
   └───────────────┘          └─────────────────┘
```

---

## 它解决什么问题

用 LLM/Agent 操作 UE 编辑器时，最难的不是"发出一个操作"，而是**知道这个操作到底生效了没有**。常见的三种翻车方式：

| 翻车方式 | 现象 | UHA 的应对 |
|---|---|---|
| **假绿** | 接口回了 `success=true`，实际什么都没发生，Agent 却报告"完成" | 记下写入返回值不算证据，**重新读状态**再比对；缺证据记 `skipped`，绝不记 `passed` |
| **假红** | 一条没有鉴别力的弱检查判失败，害得 Agent 白折腾 | 弱检查降级为 `advisory`（跳过而非失败），并说明为什么它不具鉴别力 |
| **重复生效** | 动作其实成功了、被判失败后换条通道**又做一遍** | 不可幂等动作（如相对位移）在宣告失败前**必须先重新读状态确认** |
| **错账** | 一笔错误的账（本地代码 bug、任务与通道不匹配）被当成"通道不健康"，把好通道冷却隔离 | 只有**真失败**才记账；`UNEXPECTED` 与 `NOT_SUPPORTED` 明确排除，并留一条 `health_skip` 记录 |

第三种是最危险的：它会**悄悄改坏工程**，而且改完之后往往还能"通过"验收。本项目里真实发生过一次（构件被多下移 400cm 沉进地面，而"沉进地面"不是浮空，最终复验反而通过）。完整复盘见 [`docs/FINDINGS.md`](docs/FINDINGS.md#f3)。

第四种最隐蔽：它不破坏场景，但会让框架**自己把自己锁死**（跑一遍测试 → 最优通道进冷却 → 没有任何可用的执行方式）。复盘见 [`docs/FINDINGS.md`](docs/FINDINGS.md#f5)。

---

## 核心设计取舍

### 1. 只有两类通道，没有第三类

- **结构化通道**（Unreal MCP / UE Python / Commandlet）：能给出精确值，**可程序化复验**。
- **GUI / 视觉通道**（鼠标 / 键盘 / 截图 / 混合）：能做人看得见的事，但**返回值不可信**。

结构化通道能做的事，一律走结构化通道。GUI 只在"结构化够不着"时才上（第三方插件面板、右键菜单、视口拖拽）。

### 2. 视觉只承担两件事，不承担"判断"

"这个东西是不是浮空"有**确定答案**（包围盒底边离地高度），所以它是算术题，不是视觉题。截图负责：

1. 给"任务完成"留一张**可归档的证据图**；
2. 用前后帧差异证明"画面确实变了"（纯像素运算，可复现）。

含糊的判断进不了 CI，所以不让视觉模型去承担结论。

### 3. `passed` / `failed` / `skipped` 三态严格分开

- `passed` —— 真的验了，结论是通过
- `failed` —— 真的验了，结论是不通过 → 触发降级
- `skipped` —— **证据不足，没验成** → 如实上报，不算通过也不算失败

很多自动化脚本把 `skipped` 当 `passed`，这就是假绿的来源。

### 4. 不可信就拒绝动手

检测器给出结论时会带一个 `actionable` 标志。只有它为真，框架才允许拿这个结论去**自动修改场景**。否则相关的自动修复步骤会被前置条件拦住并报错——**宁可这一步失败得清清楚楚，也不拿一个已知失准的结论去改工程**。

---

## 快速开始

```bash
# 0. 依赖（本机已备 .venv / .venv-mcp，按需重建）
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt      # 若存在，否则按 src 里的 import 装

# 1. 环境自检：哪些通道在线？
python uha.py doctor

# 2. 看有哪些 skill 可用
python uha.py list

# 3. 只出计划，不执行（能先看就先看）
python uha.py plan "把「Hall_Floor」抬高 20，然后保存"

# 4. 执行单个 skill
python uha.py run --skill actor_move --actor Hall_Floor --axis z --delta 20

# 5. 两个端到端演示
python uha.py demo1 --actor Hall_Floor --delta 20
python uha.py demo2 --ground-ref EXT_Plaza_Central --region="-7500,-6500,-3700,-2700" --delta 400

# 6. 离线回归测试（不需要 UE）
python tests/test_offline.py     # 195 项
python tests/test_router.py      #  24 项
```

**Demo2 为什么必须给 `--ground-ref`**：这是本项目最硬的一条实测结论。见下面一节。

---

## 一条值得单独说的实测结论

用户最初的设想是"全场景自动找出浮空的东西"。在真实的阿房宫场景（1184 个 Actor）上实测了四种几何做法，**全部失败**：

| 方法 | 判为浮空 | 误报率 |
|---|---|---|
| 全局中位数当地面 | 569 / 1179 | 48.3% |
| 全局第 5 分位当地面 | 1117 / 1179 | 94.7% |
| 局部邻域低分位 | 909 / 1179 | 77.1% |
| 物理定义（正下方支撑面） | 582 / 1179 | 49.4% |

真值是**没有任何东西浮空**（那栋楼本来就是多层的：地坪≈0、主殿台面≈856、屋顶≈2000+）。

于是框架的结论是：**仅凭包围盒几何，无法在建筑场景里做全场景自动浮空检测**。与其提供一个 50% 误报率、却"看起来能自动干活"的检测器，不如把检测限定在语义明确的场合：

- 地面从**显式参照物**取（不给就挂 `unguided` 警告，并禁止自动修复）
- 可选限定**巡检区域**（开放广场这类没有竖向堆叠的地方）
- 结论不可信时由上层**拒绝**自动修复

数据和四种方法的实测过程记在 [`docs/FINDINGS.md`](docs/FINDINGS.md#f1)。

---

## 目录结构

```
uha.py                      CLI 入口
config/agent.config.json    运行时配置（通道地址、ROI、权重、预算）
src/
  core/       错误类型（带稳定 code）、配置、运行日志
  adapters/   Unreal MCP / UE Python / Commandlet / Computer Use 适配器
  router/     intent（意图）· rules（规则）· cost（成本）· fallback（降级）· router
  scheduler/  Executor：把路由/降级/验证/日志缝成闭环
  skills/     actor_find · actor_move · actor_inspect · level_save · visual_inspect
  planner/    Plan / PlanStep（线性、显式，不做动态重规划）
  vision/     judge（几何判定）· image（像素差分）· geometry · grounding
  validation/ CheckResult（三态）· verifiers
tests/        test_offline.py（不需要 UE）· test_router.py
tools/        observe.py —— 独立只读观测器，用来跟框架的报告对账
              probes/    —— 一次性诊断脚本（FINDINGS 里每条结论都能重跑出来）
docs/         DESIGN · FINDINGS · DEMO · ROADMAP · FINAL_REPORT
logs/runs/    每次运行一份 jsonl，逐步骤可复盘
artifacts/    截图等产物
```

---

## 文档

| 文档 | 内容 |
|---|---|
| [`docs/DESIGN.md`](docs/DESIGN.md) | 分层架构、数据流、关键设计取舍与理由 |
| [`docs/FINDINGS.md`](docs/FINDINGS.md) | **实测发现**：五条踩坑记录与数据（含两次"看起来正常其实错了"的复盘） |
| [`docs/DEMO.md`](docs/DEMO.md) | Demo1 / Demo2 的复现步骤、真实输出与故障排查 |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | 第一版没做的事，以及为什么 |
| [`docs/FINAL_REPORT.md`](docs/FINAL_REPORT.md) | 阶段总结：交付了什么、验证到什么程度、还差什么 |

---

## 已知限制

- **HYBRID 的整屏画面差分不具鉴别力**：把 80cm 构件移动 400cm，全分辨率变化占比只有 1.35e-5，缩到 320px 宽后连同阈值都够不到。该检查现为 `advisory`，权威证据是结构化回读。
- **UE 内部视口截图与桌面截图是两回事**：前者干净、无窗口干扰，但需要 MCP 的 `vision` 域；后者能看到编辑器 UI，但需要 Computer Use 在线。
- **GUI 通道需要标定**：`vision.roi.*` 与 `desktop.editor_focus_point` 未标定时，GUI 相关步骤会**明确报错降级**，而不是盲点。代价是 `level_save` 每次先按 Ctrl+S 失败一次再降级到保存 API（多花约 2 s）。
- **通道可用性的粒度偏粗**：现在只回答"GUI 在线吗"，而实际需要的是"World Outliner 的 ROI 标了吗 / 焦点点标了吗"。所以 Router 会先选一个注定要报错的 GUI 方式再降级。计划按能力（capability）而不是按通道报可用性。
- **多对象批量操作未实现**：`max_actors_per_batch` 只有安全策略，没有批量执行路径。
- **相对位移的"精确落回地面"未实现**：需要射线检测 / 包围盒对齐，属第二版。
- **健康度是全局成功率**：会把"通道不擅长的任务"混进账本（`NOT_SUPPORTED` 已排除，但仍应按 `任务类型 × 通道` 分开记）。

---

## 许可

本仓库自有代码以 MIT 发布，见 [`LICENSE`](LICENSE)。
第三方依赖各自遵循其原许可（详见 LICENSE 末尾的第三方说明）。


## UAH 桌面进度条（2026-09-28）

运行 `start_uah_hud.cmd` 启动 HUD，运行 `test_uah_progress.cmd` 执行无 UE 的自动验收。
`demo_uah_progress.cmd --scenario success` 可显示明确标记的演示任务；它不操作 UE。
进度现在按实际验证完成量显示，包含任务切换清空、暂停回执、离线检测、多屏定位与只读设置。
完整测试方法和实机验收范围见 [UAH 进度条完善与测试说明](docs/UAH_PROGRESS_COMPLETION.md)。
