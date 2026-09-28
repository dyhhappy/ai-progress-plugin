"""UAH 视觉令牌 —— UI 概念图（UAH 桌面悬浮助手 UI · 初版）的代码化。

规则（需求 §八）：

* 深蓝黑底、现代、轻量、半透明/玻璃质感、少量冷蓝强调；
* 安全相关才用橙/红；圆角；细边框；克制阴影；
* 不做赛博朋克、不做电竞 RGB、不做传统 WinForms 工具感。

状态色（需求 §十）刻意**不**用「ERROR=红」这种一键映射思维：
颜色只是辅助，主表达仍是文字 + 状态灯 + Attention 层级。
所有颜色都在这里定义一次，宿主不得自带色表（有测试守这条）。
"""

from __future__ import annotations

# --- 面板 / 表面 ---------------------------------------------------------------

#: 窗口底色（概念图的深蓝黑）
BG = "#0B1220"
#: 卡片/面板表面（略亮一档，模拟玻璃层）
SURFACE = "#111A2B"
#: 更高的表面（展开态分区、按钮底）
SURFACE_2 = "#16213A"
#: 细边框
BORDER = "#1E2B45"
#: 边框（强调/悬停）
BORDER_STRONG = "#2C3E63"

# --- 文本 ---------------------------------------------------------------------

TEXT = "#E6EDF7"
TEXT_DIM = "#93A4C0"
TEXT_MUTED = "#5F7291"

# --- 强调 ---------------------------------------------------------------------

ACCENT = "#4C8DFF"          # 冷蓝强调
ACCENT_DIM = "#2A4A80"

# --- 功能色（安全/状态） -------------------------------------------------------

OK = "#34C759"              # 完成
INFO = "#4C8DFF"            # 运行中（蓝）
WAIT = "#F5C044"            # 思考 / 等待（暖黄）
NOTICE = "#FF8A3D"          # 需要人工注意（橙）
DANGER = "#FF5252"          # 危险 / 紧急（红）
IDLE = "#9AA7B8"            # 待机（灰白）
MUTED = "#6B7A91"

#: 危险红底（Alert 横幅）
DANGER_BG = "#3A1416"
#: 注意橙底
NOTICE_BG = "#3A2410"
#: 完成绿底（用于按钮/角标，谨慎使用）
OK_BG = "#12301C"

#: 玻璃感：面板顶部一道更亮的 1px（模拟玻璃受光边缘）
GLASS_EDGE = "#2A3A57"
#: 高优先级提示条底色（概念图 03 的红条）
ALERT_STRIP_BG = "#4A1116"
ALERT_STRIP_TEXT = "#FFD9DC"
#: 次级面板（tab 未选中 / 按钮底）
SURFACE_3 = "#0E1728"
#: 选中态（tab / 图标按钮 hover）
SELECTED_BG = "#1D2C49"


# --- 状态 → 颜色 / 文案（中文 UI，需求 §八） ----------------------------------

from ..core.models import Status  # noqa: E402
from ..core.attention import Attention  # noqa: E402

STATUS_COLORS: dict[Status, str] = {
    Status.WARNING: NOTICE,
    Status.IDLE: IDLE,
    Status.STARTING: INFO,
    Status.RUNNING: INFO,
    Status.RETRYING: INFO,
    Status.WAITING_INPUT: WAIT,
    Status.WAITING_APPROVAL: WAIT,
    Status.PAUSED: IDLE,
    Status.ERROR: DANGER,
    Status.BLOCKED: NOTICE,
    Status.DONE: OK,
    Status.CANCELLED: MUTED,
    Status.UNKNOWN: MUTED,
}

STATUS_LABELS_ZH: dict[Status, str] = {
    Status.WARNING: "验证不完整",
    Status.IDLE: "待机中",
    Status.STARTING: "启动中",
    Status.RUNNING: "运行中",
    Status.RETRYING: "重试中",
    Status.WAITING_INPUT: "等待输入",
    Status.WAITING_APPROVAL: "等待批准",
    Status.PAUSED: "已暂停",
    Status.ERROR: "出错",
    Status.BLOCKED: "被卡住",
    Status.DONE: "已完成",
    Status.CANCELLED: "已取消",
    Status.UNKNOWN: "未知状态",
}

#: 状态灯字形（几何形而不是 emoji：概念图里是纯粹的圆点/方点）
STATUS_GLYPHS_TK: dict[Status, str] = {
    Status.IDLE: "○",
    Status.STARTING: "◌",
    Status.RUNNING: "●",
    Status.RETRYING: "●",
    Status.WAITING_INPUT: "◐",
    Status.WAITING_APPROVAL: "◐",
    Status.PAUSED: "❙❙",
    Status.ERROR: "■",
    Status.BLOCKED: "▲",
    Status.DONE: "✓",
    Status.CANCELLED: "⊘",
    Status.UNKNOWN: "?",
}

#: Attention 颜色：层级越高越刺眼，但颜色只做辅助（需求 §六）
ATTENTION_COLORS: dict[int, str] = {
    0: IDLE,
    1: ACCENT,
    2: WAIT,
    3: WAIT,
    4: NOTICE,
    5: DANGER,
}


def status_color(status: Status) -> str:
    return STATUS_COLORS.get(status, MUTED)


def status_label(status: Status) -> str:
    return STATUS_LABELS_ZH.get(status, status.value)


def status_glyph(status: Status) -> str:
    return STATUS_GLYPHS_TK.get(status, "?")


def attention_color(level: int) -> str:
    try:
        n = int(level)
    except (TypeError, ValueError):
        n = 0
    return ATTENTION_COLORS.get(max(0, min(5, n)), IDLE)


# --- 布局 / 尺寸（逻辑像素；DPI 缩放由 host 统一乘） ---------------------------

#: 品牌区（所有形态共用同一套构成，见 uah/ui/brand.py）
BRAND_STAMP_SIZE = 28        # OC 印章边长（精修轮 22→28）
BRAND_DOT_SIZE = 13          # 状态灯字形字号（11→13）
BRAND_GAP = 6                # 灯 / UAH / OC 之间间距

#: 形态逻辑尺寸。精修轮整体放大：Mini/Compact/Alert ≈ +25%，Expanded ≈ +15%。
#: 实际像素 = 逻辑像素 × DPI 缩放（见 uah/ui/dpi.py）。
FORM_SIZES: dict[str, tuple[int, int]] = {
    "mini": (210, 54),
    "compact": (452, 104),
    # 展开态：品牌行 + 活动行 + tab 条 + tab 内容(最近活动) + 按钮行
    "expanded": (440, 360),
}

#: 高优先级提示条（Alert，贴附在窗口内）：加高以容纳"问题 + 动作 + 热键胶囊"
ALERT_STRIP_HEIGHT = 44
#: 文案较长（含急停热键）时用两行高度，宁可多占高度也不裁字
ALERT_STRIP_HEIGHT_2LINE = 68

#: 图标字形（零依赖、随字体缩放，见 theme/icons/README.md）
ICONS: dict[str, str] = {
    "pause": "\u2759\u2759",
    "resume": "\u25b6",
    "expand": "\u2922",
    "collapse": "\u2921",
    "chevron": "\u2304",
    "stop": "\u25a0",
    "settings": "\u2699",
    "minimize": "\u2014",
    "close": "\u2715",
    "dot_idle": "\u25cb",
    "dot_active": "\u25cf",
}

#: 图标按钮的中文悬停提示（保证"图标排版 + 中文可懂"同时成立）
ICON_HINTS: dict[str, str] = {
    "pause": "暂停任务",
    "resume": "继续任务",
    "expand": "展开面板",
    "collapse": "收起面板",
    "stop": "停止任务",
    "settings": "设置",
    "minimize": "最小化到 Mini",
    "close": "关闭 HUD",
}

RADIUS = 12
PAD = 12
FONT_FAMILY = "Microsoft YaHei UI"

#: 间距令牌（逻辑像素）。宿主不再散写 `int(10 * scale)` 这类魔数：
#: 外框尺寸 / 内边距 / 行高 / 按钮 / 图标全部由这些令牌经 px() 推出。
SPACE_XS = 4
SPACE_S = 8
SPACE_M = 12
SPACE_L = 16
ROW_H = 22          # kv 行高
BTN_PAD_X = 10      # 文本按钮左右内边距
BTN_PAD_Y = 3
ICON_PAD_X = 7      # 图标按钮左右内边距
ICON_PAD_Y = 2


def px(value: float, scale: float = 1.0) -> int:
    """逻辑像素 → 物理像素（四舍五入）。所有尺寸走这一个函数，便于统一调比例。"""
    return int(round(float(value) * float(scale)))


#: 字体（（family, size, style）元组，tkinter 直接可用）。精修轮整体 +1 级。
FONT_TITLE = (FONT_FAMILY, 11, "bold")
FONT_BODY = (FONT_FAMILY, 10)
FONT_SMALL = (FONT_FAMILY, 9)
FONT_TINY = (FONT_FAMILY, 8)
FONT_ICON = (FONT_FAMILY, 12)
FONT_MONO = ("Consolas", 9)
FONT_ALERT_TITLE = (FONT_FAMILY, 11, "bold")
FONT_HOTKEY = ("Consolas", 11, "bold")

#: 提醒/闪烁时长
FLASH_SECONDS = 6.0

# --- 执行方式的中文展示（概念图只分 Computer Use / Script / MCP 三类） --------

METHOD_LABELS_ZH: dict[str, str] = {
    "MOUSE": "Computer Use · 鼠标",
    "KEYBOARD": "Computer Use · 键盘",
    "HYBRID": "Computer Use · 混合",
    "VISION": "Computer Use · 视觉",
    "UE_PYTHON": "Script · UE Python",
    "UE_COMMANDLET": "Script · Commandlet",
    "UNREAL_MCP": "MCP · Unreal",
}


def method_label(tool: str | None) -> str:
    """执行方式 → 中文展示。认不出来的原样返回（不编造）。"""
    if not tool:
        return ""
    key = str(tool).strip().upper()
    if key in METHOD_LABELS_ZH:
        return METHOD_LABELS_ZH[key]
    for raw, label in METHOD_LABELS_ZH.items():
        if raw.lower() in str(tool).lower():
            return label
    return str(tool)

__all__ = [
    "BG", "SURFACE", "SURFACE_2", "SURFACE_3", "SELECTED_BG", "BORDER", "BORDER_STRONG",
    "GLASS_EDGE",
    "TEXT", "TEXT_DIM", "TEXT_MUTED",
    "ACCENT", "ACCENT_DIM",
    "OK", "INFO", "WAIT", "NOTICE", "DANGER", "IDLE", "MUTED",
    "DANGER_BG", "NOTICE_BG", "OK_BG", "ALERT_STRIP_BG", "ALERT_STRIP_TEXT",
    "STATUS_COLORS", "STATUS_LABELS_ZH", "STATUS_GLYPHS_TK", "ATTENTION_COLORS",
    "status_color", "status_label", "status_glyph", "attention_color",
    "FORM_SIZES", "RADIUS", "PAD", "FONT_FAMILY",
    "SPACE_XS", "SPACE_S", "SPACE_M", "SPACE_L", "ROW_H",
    "BTN_PAD_X", "BTN_PAD_Y", "ICON_PAD_X", "ICON_PAD_Y", "px",
    "BRAND_STAMP_SIZE", "BRAND_DOT_SIZE", "BRAND_GAP", "ALERT_STRIP_HEIGHT", "ALERT_STRIP_HEIGHT_2LINE",
    "ICONS", "ICON_HINTS",
    "FONT_TITLE", "FONT_BODY", "FONT_SMALL", "FONT_TINY", "FONT_ICON", "FONT_MONO",
    "FONT_ALERT_TITLE", "FONT_HOTKEY",
    "FLASH_SECONDS", "METHOD_LABELS_ZH", "method_label",
]
