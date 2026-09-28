# UAH v2 UI/UX 精修报告（UAH_UI_V2_REFINEMENT_REPORT）

> 分支：`feature/uah-v2`　时间：2026-09-22（第二轮：视觉比例 / OC 附着与清晰度 / Alert 层级 / DPI）
> 输入材料：① UAH UI 概念图（视觉 Source of Truth）② OC 原始设定图（角色 Source of Truth）
> 本轮**只做精修**：Protocol / StateStore / Hub / SSE / Adapter / Reconnect / Approval /
> Multi-Agent / Safety / Emergency Stop **全部未动**（除下述两个真实缺陷修复）。

---

## 1. 本轮调整前的问题（实测，不是感觉）

| # | 问题 | 实测证据 |
|---|---|---|
| 1 | UI 本体偏小 | 逻辑尺寸 mini 172×38 / compact 380×76（150% 下 258×57 / 570×114）；`FORM_SIZES` 原值 |
| 2 | OC 辨识度不足 | 原用**全身 Q 版**裁切，28 逻辑像素（150% 下 42px）时角色糊成一团（见 `artifacts/uah_v2_hud/11_oc_stamp_zoom_*` 的旧版对比） |
| 3 | OC 缩放路径差 | 只做 tk **整数子采样**，且只有 128/256 两档源图：150% 下 128/4=32px（块状） |
| 4 | Alert 与工作态结构雷同 | 提示条只有 1 行文字；层级只靠颜色差 |
| 5 | 内容被裁 | 字体/图标放大后，compact 内容需求高度 111px > 令牌高度 94px（会裁掉底部活动条） |
| 6 | 面板漏边 | 内容把窗口撑宽（如 678 > 逻辑 660），而面板只按逻辑尺寸绘制 → 右侧露出一条桌面 |
| 7 | **HUD 抢前台** | `build()` 后 `GetForegroundWindow() == HUD 窗口`：`WS_EX_NOACTIVATE` 只阻止*将来*被激活，首次显示仍被系统激活，且事后补样式不会把焦点还回去 |

## 2. UI 尺寸如何变化

逻辑像素（实际像素 = 逻辑 × DPI 缩放；`uah/theme/tokens.py::FORM_SIZES`）：

| 形态 | 精修前 | 精修后 | 幅度 |
|---|---|---|---|
| Mini | 172×38 | **210×54** | +22% × +42% |
| Compact | 380×76 | **452×104** | +19% × +37% |
| Expanded | 380×316 | **440×360** | +16% × +14% |
| Alert 提示条 | 34 / 52 | **44 / 68** | +29% × +31% |
| OC 印章 | 22 | **28** | +27% |
| 状态灯字号 | 11 | **13** | +18% |
| 圆角 | 10 | **12** | +20% |
| 字体（标题/正文/小/极小/图标/等宽） | 10/9/8/7/10/8 | **11/10/9/8/12/9** | 各 +1pt |

同步调整的还有：内边距、行高、按钮与图标内边距、活动条厚度（2→3px）、提示条竖条（3→4px）。
**这些不再散落在宿主里写死**：全部走新增的间距令牌 `SPACE_XS/S/M/L`、`BTN_PAD_X/Y`、
`ICON_PAD_X/Y` 与 `tokens.px(value, scale)`（宿主中已无 `int(N * scale)` 魔数）。

窗口尺寸规则：**令牌尺寸是下限，内容实测需求是上限保护** ——
`窗口高 = max(令牌高, 内容实测高)`，只在尺寸真的变化时才调用 `geometry()`（避免每 tick 动窗口）。

## 3. OC 放到了哪里

* 位置仍是**品牌区**，与需求首选方案一致：
  `[状态灯] UAH [OC 印章] | 状态内容`（Mini / Compact / Expanded / Alert 四种形态完全一致）；
* 由 `uah/ui/brand.py::BrandBar` 唯一实现，宿主不得自行拼一套；
* 印章尺寸 22 → **28 逻辑像素**（150% 下 42px），与 `UAH` 字重、状态灯形成同一视觉组；
* **不遮挡**状态信息与暂停/展开/停止按钮（右侧操作簇与 OC 之间有空隙，150% 下实测无重叠）；
* **不替代**状态灯与 `UAH` 字样（两者是主体，OC 是附加印章）。

## 4. OC 如何保证清晰度

三条措施（都不是"把图拉大"）：

1. **换裁切区域**：品牌印章改用**半身像**（`oc_bust_*`，从 OC 原始设定图裁切 head+shoulders）。
   42px 下可辨认：银白偏冷蓝长发、异色瞳（思考姿态两眼一红一金）、困困半睁眼、黑白蓝服装轮廓。
   全身姿态（`oc_stamp_*`）保留给较大展示场景。
   → 生成脚本 `uah/theme/oc/build_stamps.py`，比例常量 `BUST_BOX=(0.26,0.74,0.00,0.56)`（可复跑，**零重绘**）。
2. **多档预渲染**：每状态预生成 6 档（48/72/96/128/176/256），运行时挑**最小且不小于目标**的一档。
3. **精确缩放**：拿到源图后用 Pillow **LANCZOS** 精确缩到目标物理像素（`uah/ui/oc.py::_fit`）；
   没有 Pillow 时退回 tk 整数子采样。**两条路径都不放大**（源比目标小就保持原样）。

素材规模：96 个印章 PNG（8 状态 × 6 档 × {半身, 全身}）+ 5 个原始姿态裁切 + 原图（`source/oc_sheet.jpg`）。

## 5. Alert 如何强化

与工作态拉开层级的三处硬手段（不只是换颜色）：

1. **独立结构**：左侧 **4px 竖条**（贯穿整条 + 面板高度）+ 面板外框与描边转为橙/红；顶部提示条与卡片本体物理分离；
2. **三段信息**：`高优先级提示：<问题标题>`（11pt 粗体，危险色） / **具体问题描述** / **用户动作**「请使用快捷键紧急停止」；
3. **热键胶囊**：`CTRL + ALT + F12` 用等宽粗体 + **1px 危险色边框** + 高对比底色单独成块——"按什么"一眼可见；
4. **高度实测**：提示条高度按内容实测（3 行 × DPI），不再被裁（旧版曾出现热键被裁成 `CTRL + ALT + F1`）。

层级关系（颜色 + 边框 + 文案 + 布局 + 提醒区共同表达）：
`待机(灰) → 工作中(蓝) → 等待用户(黄, L3 不弹条) → 需要处理(橙条 L4) → 安全紧急(红条 L5, 优先讲安全)`。

## 6. DPI 如何处理

* 新增 `uah/ui/dpi.py`：**唯一**的缩放系数来源与 DPI 感知开关；
  `UAH_HUD_SCALE` 环境变量可强制缩放系数（用于 100/125/150% 真实验证，不改系统设置）；
* 所有尺寸走 `tokens.px(value, scale)`；字体由 tk 按 DPI 自动缩放，宿主只缩放像素尺寸；
* 窗口高/提示条高按**实测内容**兜底，因此在三种缩放下都不会裁切；
* 实测（本机 150% 原生，另用环境变量覆盖验证 100%/125%）：

| 缩放 | Compact 物理尺寸 | 内容需求 vs 窗口 | 结果 |
|---|---|---|---|
| 100% | 452 × 111 | 435×111 ≤ 452×111 | 不裁切 |
| 125% | 565 × 130 | 458×125 ≤ 565×130 | 不裁切 |
| 150% | 678 × 156 | 484×145 ≤ 678×156 | 不裁切 |

## 7. 修改文件

| 文件 | 改动 |
|---|---|
| `uah/theme/tokens.py` | 尺寸/字体全部放大；新增间距令牌与 `px()`；新增 `FONT_ALERT_TITLE` / `FONT_HOTKEY` |
| `uah/theme/oc/build_stamps.py` | 新增半身像裁切（`BUST_BOX`）与 6 档尺寸（48–256）；清单补 `kind` |
| `uah/theme/oc/oc_bust*.png`（新增 48 个） | 半身像品牌印章（8 状态 × 6 档） |
| `uah/ui/oc.py` | 品牌印章改用 `oc_bust_*`；新增 `resolve_asset(..., kind=)`；`_fit()` 精确缩放（Pillow LANCZOS，禁止放大） |
| `uah/ui/dpi.py`（新增） | 缩放系数 + DPI 感知（`UAH_HUD_SCALE` 覆盖） |
| `uah/hosts/desktop/compact.py` | 间距令牌化（37 处）；Alert 三段式结构 + 热键胶囊；内容驱动尺寸；面板覆盖实际窗口；**前台归还**（`_restore_foreground`）；ctypes 签名修正 |
| `uah/tests/v2_hud_smoke.py` | 50 项：新增 DPI 矩阵（含不裁切断言）、热键胶囊/竖条/动作文案断言、OC 真实素材断言与截图 |
| `uah/tests/compact_smoke.py` | 焦点断言改为不变量（HUD 不得成为前台窗口）；尺寸断言改为"宽度精确 + 高度不低于下限" |
| `docs/UAH_UI_V2_REFINEMENT_REPORT.md` | 本报告 |

## 8. 测试结果（全部真实运行）

| 套件 | 结果 |
|---|---|
| `uah/tests/v2_hud_smoke.py`（真 Hub + 真 SSE + 真窗口 + 真截图） | **50 / 50** |
| `uah/tests/compact_smoke.py`（真实窗口交互） | **22 / 22** |
| `uah/tests/test_uah_phase1.py`（单元 + 验收自查，含 UHA 回归 388 项） | **152 / 152** |
| `uah/tests/v2_integration.py`（真 Executor/Controller/Hub/Safety） | **44 / 44** |
| `uah/tests/v2_safety_hotkey.py` | **7 / 7** |
| `uah/tests/gui_smoke.py`（Debug 宿主） | **19 / 19** |

## 9. 截图（全部取自真实运行窗口，非设计稿）

| 文件 | 内容 |
|---|---|
| `artifacts/uah_v2_hud/09_mini.png` | Mini 实机截图（210×54 逻辑，150% 下 315×81） |
| `artifacts/uah_v2_hud/02_compact_running.png` | Compact 实机截图（150%，678×156） |
| `artifacts/uah_v2_hud/10_compact_100pct.png` / `125pct` / `150pct` | 三种 DPI 下的 Compact（同机对比） |
| `artifacts/uah_v2_hud/03_expanded_running.png` | Expanded 实机截图（当前任务页） |
| `artifacts/uah_v2_hud/04_alert_l5.png` | Alert L5 实机截图（红边 + 竖条 + 三段 + 热键胶囊） |
| `artifacts/uah_v2_hud/05_alert_l4.png` | Alert L4 实机截图（橙） |
| `artifacts/uah_v2_hud/11_oc_stamp_zoom_150pct.png` | **OC 印章清晰度**（真实截图裁切放大，半身像可辨认） |
| `artifacts/uah_v2_hud/06_oc_brand_crop.png` | OC 已真实接入品牌区（状态灯 / UAH / OC 三者并存） |

## 10. 已决定项与未解决问题

### 10.1 急停热键：**正式定为 Ctrl+Alt+F12**（已决定，实现已对齐）

概念图/早期文本写的是 Ctrl+Alt+Esc，本轮按"保留既有机制"确认采用 **Ctrl+Alt+F12**，
并把三处默认值统一（此前只有真实配置里是它，代码默认值还留着带 Shift 的老组合）：

| 位置 | 改动 |
|---|---|
| `config/agent.config.json` | 已是 `ctrl+alt+f12`（不变） |
| `config/agent.config.example.json` | **新增**显式 `desktop.emergency_hotkey: "ctrl+alt+f12"`（让决定可见） |
| `src/core/config.py` | 内置默认 `ctrl+alt+shift+f12` → **`ctrl+alt+f12`**（这是真正的不一致点：配置缺键时原本会退到带 Shift 的组合） |
| `src/desktop/hotkey.py` | `EmergencyHotkey` 默认值、`parse_hotkey` 的修饰键 fallback 均改为 Ctrl+Alt（**不再补 Shift**） |

UAH 侧无需改动：提示条与「设置」页显示的仍是**真实快照值**（`SafetySnapshot.hotkey`）。
测试里已加断言锁死这三个默认值（`uah/tests/v2_safety_hotkey.py`，共 9 项）。

### 10.2 `libpng warning: tRNS: invalid with alpha channel` —— 与我们的素材无关

实测结论：**裸 `tk.Tk()` + `update()` 也会打印这 5 条提示**（Tcl/Tk 自带的图像资源），
而本项目生成的 96 个印章 PNG 里**没有** tRNS 块（chunk 只有 IHDR/IDAT/IEND）。
因此该项不是素材或加载方式的问题，无需改动；也不建议屏蔽 stderr（会掩盖真实错误）。

### 10.3 仍未解决
3. Expanded「设置」页仍只覆盖静音/形态/关闭/热键说明——概念图里的"任务域/权限"类设置涉及 UHA 配置语义，未接（不造假开关）。
4. **多显示器 / 负坐标屏**未专门处理（按主屏夹取），未测。
5. 玻璃感是"深色 + α=0.94 + 1px 受光边"的近似：tkinter 无法做真实背景模糊。
6. 本轮**未生成**任何设计展示板/概念海报：交付物就是真实运行的 UI（概念图仅作视觉参照）。
