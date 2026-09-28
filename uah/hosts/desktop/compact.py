"""UAH v2 桌面 HUD —— Mini / Compact / Expanded / Alert 四形态。

视觉 Source of Truth：用户提供的 UI 概念图（UAH 桌面悬浮助手 UI）。
OC 的长相 Source of Truth：用户提供的 OC 原始设定图（素材由
``uah/theme/oc/build_stamps.py`` 裁切生成，**零重绘**）。

四形态职责：

    Mini      长期常驻最小形态：状态灯 + UAH + OC + 展开（概念图 05）
    Compact   默认主形态：状态 / 环境 / 主要动作 / 关键操作（概念图 02、04）
    Expanded  展开面板，分 当前任务 / 执行方式 / 设置 三页（概念图 06）
    Alert     高优先级提示**贴附在窗口内**：竖条 + 提示行 + 真实热键（概念图 03）

工程约束（都不许破坏）：

* 颜色/尺寸/文案一律取自 ``uah/theme/tokens.py``（本文件不硬编码颜色）；
* 品牌区一律用 ``uah/ui/brand.py::BrandBar``（三形态共用同一套构成）；
* 一切数据来自 Hub 事件流：**不猜状态**、**不编文案**、**不造百分比**；
* 暂停/继续/停止走真实控制通道（Hub ``/control`` → ControlPoller → SessionController）；
* HUD 内**没有**急停按钮：急停只走键盘热键，这里只显示真实热键。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from .hud import DashboardApp, default_state_path  # noqa: F401 - 复用提醒配置路径
from ...core.models import Status
from ...theme import tokens as T
from ...ui.brand import BrandBar

#: 安全条目的 agent id（由 uah/safety/bridge.py 定义；这里只做识别）
SAFETY_AGENT_ID = "uha-safety"

#: 私有：窗口透明键色（用于圆角掩码）。选一个不会被用到的近黑蓝。
_KEY_COLOR = "#010203"

#: 活动指示条动画步长（毫秒）。只在"正在工作且窗口可见"时跑。
_ACTIVITY_STEP_MS = 120

#: 展开面板的页签（概念图 06 的 tab 条）
TABS: tuple[tuple[str, str], ...] = (("task", "当前任务"), ("method", "执行方式"),
                                     ("settings", "设置"))


# DPI 相关统一放在 uah/ui/dpi.py（缩放系数 + 感知开关），宿主只调用不自己算。
# 这里再导出一次，兼容既有测试 `from uah.hosts.desktop.compact import enable_dpi_awareness`。
from ...ui.progress import progress_view, task_elapsed
from ...ui.monitors import work_areas, clamp_position, tk_position
from ...ui.icons import icon_image
from ...ui.fonts import configure_fonts
from ...ui.dpi import dpi_scale, enable_dpi_awareness  # noqa: E402


class HudApp(DashboardApp):
    """独立桌面 HUD。四形态共享同一个 Hub 订阅与同一份快照。"""

    MODES = ("mini", "compact", "expanded")
    SIZES = dict(T.FORM_SIZES)

    def __init__(self, *args: Any, mode: str | None = None,
                 settings_path: str | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.settings_path = (Path(settings_path) if settings_path
                              else Path(__file__).resolve().parents[3] / ".state/uah/window.json")
        self.settings: dict[str, Any] = {}
        try:
            parsed = json.loads(self.settings_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                self.settings = parsed
        except (OSError, ValueError):
            self.settings = {}
        self.mode = str(mode or self.settings.get("mode") or "compact")
        if self.mode not in self.MODES:
            self.mode = "compact"
        self.selected: str | None = self.settings.get("selected")
        self.tab = str(self.settings.get("tab") or "task")
        if self.tab not in {k for k, _ in TABS}:
            self.tab = "task"

        self._forms: dict[str, Any] = {}
        self._brands: dict[str, BrandBar] = {}
        self._stamps: list[tuple[Any, str]] = []
        self._dots: dict[str, Any] = {}
        self._state_labels: dict[str, Any] = {}
        self._activity_labels: dict[str, Any] = {}
        self._elapsed_labels: dict[str, Any] = {}
        self._detail_widgets: dict[str, Any] = {}
        self._method_widgets: dict[str, Any] = {}
        self._settings_widgets: dict[str, Any] = {}
        self._timeline_widgets: dict[str, Any] = {}
        self._tab_buttons: dict[str, Any] = {}
        self._tab_frames: dict[str, Any] = {}
        self._safety_label: Any = None
        self._pause_buttons: list[Any] = []
        self._stop_buttons: list[Any] = []
        self._hint_label: Any = None
        self._approval_row: Any = None
        self._approval_label: Any = None
        self._approve_btn: Any = None
        self._reject_btn: Any = None
        self._scale = 1.0
        self._rounded = False
        #: 贴附式 Alert 提示条（Frame，**不是**独立窗口）
        self._alert: Any = None
        self._alert_level = 0
        self._alert_state: tuple[int, str] = (0, "")
        self._alert_dismissed_until = 0.0
        self._alert_two_lines = False
        #: build() 时是否成功把前台还给了原窗口（诊断用）
        self._foreground_restored = False
        self._user32_dll: Any = None
        #: 构造时（还没有任何 Tk 窗口）的前台窗口 —— 这才是"原来的前台"。
        #: 在 build() 里再取就晚了：Tk() 创建第一个窗口时就可能已经把自己激活。
        self._fg_before_window = self._foreground_hwnd()
        #: 提示条**实测**像素高度（内容驱动：3 行 + 不同 DPI/字体下都算得准）
        self._alert_px_height = 0
        self._timeline_rows: list[dict[str, Any]] = []
        self._timeline_at = 0.0
        self._timeline_busy = False
        self._approval_pending: list[dict[str, Any]] = []
        self._approval_busy = False
        self._approval_next = 0.0
        self._current_approval: dict[str, Any] | None = None
        self._control_token: str | None = None
        self._paused_locally = False
        self._activity_phase = 0
        self._progress_widgets = {}
        self._control_pending = None
        self._control_busy = False
        self._control_message = ""
        self._control_message_until = 0.0
        self._geometry_check_at = 0.0
        self._drag_anchor: tuple[int, int] = (0, 0)
        #: 上一次真正通过 geometry 应用的窗口尺寸（避免每 tick 重设几何）
        self._last_size: tuple[int, int] = (0, 0)

    # ==================================================================
    # 构建
    # ==================================================================

    def build(self) -> Any:
        api = enable_dpi_awareness()
        tk = self.tk
        root = tk.Tk()
        root.withdraw()
        if os.name == "nt":
            try:
                root.attributes("-disabled", True)
            except Exception:  # noqa: BLE001
                pass
        self._root = root
        root.title(self.title)
        root.attributes("-topmost", self._topmost)
        try:
            root.overrideredirect(True)
        except Exception:  # noqa: BLE001
            pass
        try:
            # 轻微半透明 → 玻璃感。0.94 在浅色背景（编辑器/白底网页）上会让底下的
            # 文字明显透进来，读状态时干扰；0.97 既保留玻璃感又保证可读性（实机对比过）。
            root.attributes("-alpha", 0.97)
        except Exception:  # noqa: BLE001
            pass

        # 缩放系数：UAH_HUD_SCALE 环境变量优先（用于 100%/125%/150% 真实验证），
        # 否则按窗口实际 DPI 计算。
        self._scale = dpi_scale(root)
        configure_fonts(root, self._scale)

        root.configure(bg=T.BG)
        try:
            root.attributes("-transparentcolor", _KEY_COLOR)
            self._rounded = True
        except Exception:  # noqa: BLE001
            self._rounded = False
        bg = _KEY_COLOR if self._rounded else T.BG

        canvas = tk.Canvas(root, bg=bg, highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True)
        self._canvas = canvas
        self._surface = tk.Frame(canvas, bg=T.BG)
        self._surface_window = canvas.create_window(0, 0, window=self._surface, anchor="nw")
        canvas.bind("<Configure>", lambda e: self._redraw_surface())

        self._forms["mini"] = tk.Frame(self._surface, bg=T.BG)
        self._forms["compact"] = tk.Frame(self._surface, bg=T.BG)
        self._forms["expanded"] = tk.Frame(self._surface, bg=T.BG)
        self._build_mini(self._forms["mini"])
        self._build_compact(self._forms["compact"])
        self._build_expanded(self._forms["expanded"])
        self._build_alert(self._surface)

        self._after_ids.append(root.after(self.poll_ms, self._drain))
        self._after_ids.append(root.after(self.clock_ms, self._tick))
        self._after_ids.append(root.after(_ACTIVITY_STEP_MS, self._animate))

        self._apply_form(self.mode, save=False)
        root.update_idletasks()
        self._no_activate()
        # 显示前记住前台窗口；优先用"构造时"记录的值（见 __init__ 注释），
        # 显示后若被自己抢走就还回去（见 _restore_foreground）
        previous_fg = self._fg_before_window or self._foreground_hwnd()
        root.deiconify()
        root.update()
        if os.name == "nt":
            try:
                root.attributes("-disabled", False)
            except Exception:  # noqa: BLE001
                pass
        root.update_idletasks()
        self._no_activate()
        self._foreground_restored = self._restore_foreground(previous_fg)
        self._dpi_mode = api
        return root

    def _user32(self) -> Any:
        """带正确签名声明的 user32。

        **必须**声明 ``restype/argtypes``：窗口句柄在 64 位下是指针宽度，
        不声明时 ctypes 会按 c_int 截断返回值 —— 拿截断过的句柄去
        ``SetForegroundWindow`` 会失败（实测：归还前台一直不生效就是这个原因）。
        """
        if getattr(self, "_user32_dll", None) is None:
            import ctypes
            from ctypes import wintypes as w

            u = ctypes.WinDLL("user32", use_last_error=True)
            u.GetForegroundWindow.restype = w.HWND
            u.GetForegroundWindow.argtypes = []
            u.SetForegroundWindow.argtypes = [w.HWND]
            u.SetForegroundWindow.restype = w.BOOL
            self._user32_dll = u
        return self._user32_dll

    def _foreground_hwnd(self) -> int:
        """当前前台窗口句柄（失败返回 0）。"""
        if os.name != "nt":
            return 0
        try:
            return int(self._user32().GetForegroundWindow() or 0)
        except Exception:  # noqa: BLE001
            return 0

    def _restore_foreground(self, previous: int) -> bool:
        """若 HUD 抢占成了前台窗口，把前台还给**原来的**窗口。

        为什么需要这一步：``WS_EX_NOACTIVATE`` 只能阻止"将来"被激活；窗口第一次
        ``deiconify()`` 时仍可能被系统激活，而事后补设扩展样式并不会把焦点还回去
        （实测：build() 之后 foreground == HUD 窗口）。HUD 常驻桌面，抢焦点会打断
        用户正在操作的 UE 视口/编辑器，所以这里显式归还。
        """
        if os.name != "nt" or self._root is None:
            return False
        me = int(getattr(self, "native_hwnd", 0) or 0)
        if not me or not previous or previous == me:
            return False
        try:
            import ctypes

            user32 = self._user32()
            if int(user32.GetForegroundWindow() or 0) != me:
                return False
            user32.SetForegroundWindow(ctypes.c_void_p(previous))
            return int(user32.GetForegroundWindow() or 0) != me
        except Exception:  # noqa: BLE001
            return False

    def _no_activate(self) -> None:
        """WS_EX_NOACTIVATE + WS_EX_TOOLWINDOW：永不抢焦点、不进 Alt-Tab。"""
        if os.name != "nt" or self._root is None:
            return
        try:
            import ctypes

            from ctypes import wintypes as w

            u = ctypes.WinDLL("user32", use_last_error=True)
            u.GetParent.argtypes = [w.HWND]
            u.GetParent.restype = w.HWND
            u.GetWindowLongW.argtypes = [w.HWND, ctypes.c_int]
            u.GetWindowLongW.restype = ctypes.c_long
            u.SetWindowLongW.argtypes = [w.HWND, ctypes.c_int, ctypes.c_long]
            hwnd = u.GetParent(self._root.winfo_id()) or self._root.winfo_id()
            style = u.GetWindowLongW(hwnd, -20)
            u.SetWindowLongW(hwnd, -20, style | 0x08000000 | 0x80)
            self.native_hwnd = hwnd
        except Exception:  # noqa: BLE001
            self.native_hwnd = None

    # -- 面板表面（圆角 + 玻璃受光边 + 告警左竖条） -------------------------

    @property
    def _alert_strip_height(self) -> int:
        """1 行 / 2 行自适应：长文案（含热键）两行显示，**不裁字**。"""
        base = T.ALERT_STRIP_HEIGHT_2LINE if self._alert_two_lines else T.ALERT_STRIP_HEIGHT
        return int(base * self._scale)

    def _form_need_height(self) -> int:
        """当前形态内容**实测**需要的高度（物理像素）。

        令牌里的 FORM_SIZES 是设计下限；字体/DPI/内容（例如审批行出现）都会改变
        真实需求高度，只按令牌算会把内容裁掉。实测值取 max 用。
        """
        frame = self._forms.get(self.mode)
        if frame is None:
            return 0
        try:
            frame.update_idletasks()
            return int(frame.winfo_reqheight())
        except Exception:  # noqa: BLE001
            return 0

    def _scaled_size(self) -> tuple[int, int]:
        w, h = self.SIZES[self.mode]
        # 令牌尺寸是**下限**，实测需求高度是上限保护 —— 图标/字体放大后不会被裁
        target_h = max(int(round(h * self._scale)), self._form_need_height())
        extra = 0
        if self._alert_level >= 4:
            extra = self._alert_px_height or self._alert_strip_height
            w = max(w, self.SIZES["compact"][0])
        return int(round(w * self._scale)), target_h + extra

    def _sync_window_size(self) -> None:
        """尺寸变了才动几何（内容驱动的高度变化走这里）。"""
        root = self._root
        if root is None or not root.winfo_exists():
            return
        size = self._scaled_size()
        if size == self._last_size:
            return
        self._last_size = size
        try:
            root.geometry(f"{size[0]}x{size[1]}")
            root.minsize(size[0], size[1])
        except Exception:  # noqa: BLE001
            pass
        # 等 Tk 落实尺寸后再画面板（否则拿到的还是旧宽高，仍会漏边）
        try:
            root.update_idletasks()
        except Exception:  # noqa: BLE001
            pass
        self._redraw_surface()

    def _redraw_surface(self) -> None:
        if self._root is None:
            return
        w, h = self._scaled_size()
        # 面板必须覆盖**窗口实际尺寸**：Tk 的窗口可能因为子控件需求被撑大 1~20px，
        # 若只按逻辑尺寸绘制，右侧/底部会漏出一条"透明带"（桌面透出来）。
        try:
            w = max(w, int(self._root.winfo_width()))
            h = max(h, int(self._root.winfo_height()))
        except Exception:  # noqa: BLE001
            pass
        c = self._canvas
        c.delete("surface_bg")
        if not self._rounded:
            c.itemconfigure(self._surface_window, width=w, height=h)
            return
        edge = T.DANGER if self._alert_level >= 5 else (
            T.NOTICE if self._alert_level == 4 else T.BORDER)
        # 精确圆角：整块面板 + 四角透明掩码（smooth polygon 会把直边也收进去，顶边漏桌面）
        c.create_rectangle(0, 0, w - 1, h - 1, fill=T.BG, outline=edge, width=1,
                           tags="surface_bg")
        c.create_line(1, 1, w - 2, 1, fill=T.GLASS_EDGE, tags="surface_bg")
        if self._alert_level >= 4:
            bar = T.DANGER if self._alert_level >= 5 else T.NOTICE
            c.create_rectangle(0, 2, max(3, T.px(4, self._scale)), h - 3,
                               fill=bar, outline=bar, tags="surface_bg")
        r = max(2, int(round(T.RADIUS * self._scale)))
        for (cx, cy) in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
            c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=_KEY_COLOR,
                          outline=_KEY_COLOR, tags="surface_bg")
        inset = max(1, int(round(r * 0.3)))
        c.coords(self._surface_window, inset, inset)
        c.itemconfigure(self._surface_window, width=max(1, w - 2 * inset),
                        height=max(1, h - 2 * inset))

    # -- 品牌区（三形态共用同一构成） --------------------------------------

    def _brand(self, parent: Any, mode: str) -> BrandBar:
        brand = BrandBar(parent, self.tk, scale=self._scale, bg=T.BG)
        self._brands[mode] = brand
        if brand.stamp is not None:
            self._stamps.append((brand.stamp, mode))
        self._dots[mode] = brand.dot
        return brand

    # -- Mini（概念图 05） -------------------------------------------------

    def _build_mini(self, frame: Any) -> None:
        tk = self.tk
        s = self._scale
        row = tk.Frame(frame, bg=T.BG)
        row.pack(fill="both", expand=True, padx=T.px(T.SPACE_M, s), pady=T.px(T.SPACE_S, s))
        brand = self._brand(row, "mini")
        brand.frame.pack(side="left")
        # 概念图 05：Mini 只有「状态灯 + UAH + OC + 展开入口」。
        # 刻意不放状态文字/进度/字段——状态由状态灯的颜色与形状表达。
        chev = tk.Label(row, text=T.ICONS["chevron"], bg=T.BG, fg=T.TEXT_DIM,
                        font=T.FONT_ICON)
        chev.pack(side="right")
        state = tk.Label(row, text="", bg=T.BG, fg=T.TEXT_DIM, font=T.FONT_SMALL, anchor="e")
        self._state_labels["mini"] = state        # 不 pack：保留挂点但不上屏
        self._bind_drag_and_click(*brand.widgets, chev,
                                  on_click=lambda: self._apply_form("compact"))

    # -- Compact（概念图 02 / 04） -----------------------------------------

    def _build_compact(self, frame: Any) -> None:
        tk = self.tk
        s = self._scale
        pad = T.px(T.SPACE_M, s)
        head = tk.Frame(frame, bg=T.BG)
        head.pack(fill="x", padx=pad, pady=(T.px(T.SPACE_S, s), 0))
        brand = self._brand(head, "compact")
        brand.frame.pack(side="left")
        sep = tk.Label(head, text="|", bg=T.BG, fg=T.BORDER_STRONG, font=T.FONT_SMALL)
        sep.pack(side="left", padx=(T.px(T.SPACE_S, s), T.px(T.SPACE_S, s)))
        elapsed = tk.Label(head, text="", bg=T.BG, fg=T.TEXT_MUTED, font=T.FONT_MONO)
        elapsed.pack(side="right")
        self._elapsed_labels["compact"] = elapsed
        state = tk.Label(head, text="等待 Agent", bg=T.BG, fg=T.TEXT, font=T.FONT_BODY,
                         anchor="w")
        state.pack(side="left", fill="x", expand=True)
        self._state_labels["compact"] = state

        body = tk.Frame(frame, bg=T.BG)
        body.pack(fill="x", padx=pad, pady=(T.px(T.SPACE_XS, s), 0))
        activity = tk.Label(body, text="", bg=T.BG, fg=T.TEXT_DIM, font=T.FONT_SMALL,
                            anchor="w")
        activity.pack(side="left", fill="x", expand=True)
        self._activity_labels["compact"] = activity
        # 关键操作：图标按钮（概念图 02 的 ⏸ ⤢ ■）。pack 顺序与视觉顺序相反。
        for icon, key, cb in (
            (T.ICONS["stop"], "stop", lambda: self._control("stop")),
            (T.ICONS["expand"], "expand", lambda: self._apply_form("expanded")),
            (T.ICONS["pause"], "pause", lambda: self._toggle_pause()),
        ):
            btn = self._icon_button(body, icon, cb, key=key, danger=(key == "stop"))
            btn.pack(side="right", padx=(T.px(T.SPACE_XS, s), 0))
            if key == "pause":
                self._pause_buttons.append(btn)
            elif key == "stop":
                self._stop_buttons.append(btn)

        track = tk.Frame(frame, bg=T.SURFACE_2, height=max(3, T.px(3, s)))
        track.pack(fill="x", padx=pad, pady=(T.px(T.SPACE_S, s), T.px(T.SPACE_S, s)))
        self._track = track
        self._thumb = tk.Frame(track, bg=T.ACCENT, height=max(3, T.px(3, s)))
        progress = tk.Label(frame, text="", bg=T.BG, fg=T.TEXT_DIM, font=T.FONT_TINY, anchor="w")
        progress.configure(wraplength=T.px(self.SIZES["compact"][0]-32, s), justify="left")
        progress.pack(fill="x", padx=pad)
        self._progress_widgets["compact"] = (track, self._thumb, progress)
        self._bind_drag_and_click(*brand.widgets, state, elapsed)

    # -- Expanded（概念图 06：三页，不做成控制台） -------------------------

    def _build_expanded(self, frame: Any) -> None:
        tk = self.tk
        s = self._scale
        pad = T.px(T.SPACE_M, s)
        head = tk.Frame(frame, bg=T.BG)
        head.pack(fill="x", padx=pad, pady=(T.px(T.SPACE_S, s), 0))
        brand = self._brand(head, "expanded")
        brand.frame.pack(side="left")
        tk.Label(head, text="|", bg=T.BG, fg=T.BORDER_STRONG,
                 font=T.FONT_SMALL).pack(side="left", padx=(T.px(T.SPACE_S, s), T.px(T.SPACE_S, s)))
        elapsed = tk.Label(head, text="", bg=T.BG, fg=T.TEXT_MUTED, font=T.FONT_MONO)
        elapsed.pack(side="right")
        self._elapsed_labels["expanded"] = elapsed
        self._icon_button(head, T.ICONS["close"], lambda: self._apply_form("mini"),
                          key="close").pack(side="right")
        state = tk.Label(head, text="等待 Agent", bg=T.BG, fg=T.TEXT, font=T.FONT_BODY,
                         anchor="w")
        state.pack(side="left", fill="x", expand=True)
        self._state_labels["expanded"] = state

        activity = tk.Label(frame, text="", bg=T.BG, fg=T.TEXT_DIM, font=T.FONT_SMALL,
                            anchor="w")
        activity.pack(fill="x", padx=pad, pady=(T.px(T.SPACE_XS, s), T.px(T.SPACE_S, s)))
        self._activity_labels["expanded"] = activity

        tabs = tk.Frame(frame, bg=T.BG)
        tabs.pack(fill="x", padx=pad)
        for key, label in TABS:
            btn = tk.Label(tabs, text=label, bg=T.SURFACE_3, fg=T.TEXT_DIM,
                           font=T.FONT_SMALL, padx=T.px(T.SPACE_M, s), pady=T.px(T.SPACE_XS, s),
                           cursor="hand2")
            btn.pack(side="left", padx=(0, T.px(T.SPACE_XS, s)))
            btn.bind("<Button-1>", lambda e, k=key: self._set_tab(k))
            self._tab_buttons[key] = btn
        tk.Frame(frame, bg=T.BORDER, height=1).pack(fill="x", padx=pad, pady=(T.px(T.SPACE_S, s), 0))

        progress = tk.Label(frame, text="", bg=T.BG, fg=T.TEXT_DIM, font=T.FONT_TINY, anchor="w")
        progress.configure(wraplength=T.px(self.SIZES["compact"][0]-32, s), justify="left")
        progress.pack(fill="x", padx=pad)
        track = tk.Frame(frame, bg=T.SURFACE_2, height=max(3, T.px(3, s)))
        track.pack(fill="x", padx=pad, pady=(0, T.px(T.SPACE_S, s)))
        thumb = tk.Frame(track, bg=T.ACCENT)
        self._progress_widgets["expanded"] = (track, thumb, progress)

        # 页 1：当前任务
        page_task = tk.Frame(frame, bg=T.BG)
        self._tab_frames["task"] = page_task
        detail = tk.Frame(page_task, bg=T.BG)
        detail.pack(fill="x", padx=pad, pady=T.px(T.SPACE_M, s))
        self._detail_widgets["task"] = self._kv_row(detail, "当前任务")
        self._detail_widgets["action"] = self._kv_row(detail, "当前动作")
        self._detail_widgets["step"] = self._kv_row(detail, "当前步骤")
        tk.Label(page_task, text="最近活动", bg=T.BG, fg=T.TEXT_MUTED,
                 font=T.FONT_TINY, anchor="w").pack(fill="x", padx=pad, pady=(T.px(T.SPACE_XS, s), 0))
        timeline = tk.Label(page_task, text="（暂无事件）", bg=T.BG, fg=T.TEXT_DIM,
                            font=T.FONT_SMALL, anchor="nw", justify="left")
        timeline.pack(fill="both", expand=True, padx=pad, pady=(T.px(T.SPACE_XS, s), 0))
        self._timeline_widgets["label"] = timeline

        # 页 2：执行方式
        page_method = tk.Frame(frame, bg=T.BG)
        self._tab_frames["method"] = page_method
        m = tk.Frame(page_method, bg=T.BG)
        m.pack(fill="x", padx=pad, pady=T.px(T.SPACE_M, s))
        self._method_widgets["method"] = self._kv_row(m, "执行方式")
        self._method_widgets["phase"] = self._kv_row(m, "当前阶段")
        self._method_widgets["attention"] = self._kv_row(m, "关注级别")
        self._safety_label = self._kv_row(m, "安全状态")
        try:
            self._safety_label.configure(wraplength=T.px(300, s), justify="left")
        except Exception:  # noqa: BLE001
            pass

        # 页 3：设置
        page_settings = tk.Frame(frame, bg=T.BG)
        self._tab_frames["settings"] = page_settings
        st = tk.Frame(page_settings, bg=T.BG)
        st.pack(fill="x", padx=pad, pady=T.px(T.SPACE_M, s))
        row1 = tk.Frame(st, bg=T.BG)
        row1.pack(fill="x", pady=T.px(T.SPACE_XS, s))
        self._settings_widgets["mute"] = self._flat_button(row1, "静音", self.toggle_mute)
        self._settings_widgets["mute"].pack(side="left", padx=(0, T.px(T.SPACE_S, s)))
        for label, mode in (("最小化", "mini"), ("简洁模式", "compact")):
            self._flat_button(row1, label, lambda m=mode: self._apply_form(m)).pack(
                side="left", padx=(0, T.px(T.SPACE_S, s)))
        self._flat_button(row1, "关闭", self.close, fg=T.DANGER).pack(side="left")
        for key, label in (("domain", "任务域"), ("execution", "执行模式"),
                           ("permissions", "权限配置"), ("approval", "审批策略")):
            value = self._kv_row(st, label)
            value.configure(wraplength=T.px(300, s))
            self._settings_widgets[key] = value
        self._settings_widgets["hotkey"] = self._kv_row(st, "急停热键")
        self._settings_widgets["hint"] = tk.Label(
            st, text="运行配置只读，修改后需重新启动任务。\n暂停按执行边界生效，不能撤回已提交操作。",
            bg=T.BG, fg=T.TEXT_MUTED, font=T.FONT_TINY, anchor="w", justify="left")
        self._settings_widgets["hint"].pack(fill="x", pady=(T.px(T.SPACE_XS, s), 0))

        # 底部：提示 + 暂停/停止（概念图 06 右下角）
        foot = tk.Frame(frame, bg=T.BG)
        foot.pack(side="bottom", fill="x", padx=pad, pady=(T.px(T.SPACE_S, s), T.px(T.SPACE_M, s)))
        self._hint_label = tk.Label(foot, text="", bg=T.BG, fg=T.WAIT,
                                    font=T.FONT_TINY, anchor="w")
        self._hint_label.pack(side="left", fill="x", expand=True)
        for text, key, cb in (
            ("收起", "collapse", lambda: self._apply_form("compact")),
            ("停止", "stop", lambda: self._control("stop")),
            ("暂停", "pause", lambda: self._toggle_pause()),
        ):
            btn = self._flat_button(foot, text, cb,
                                    fg=T.DANGER if key == "stop" else T.TEXT_DIM)
            btn.pack(side="right", padx=(T.px(T.SPACE_S, s), 0))
            if key == "pause":
                self._pause_buttons.append(btn)
            elif key == "stop":
                self._stop_buttons.append(btn)

        # 批准行（只在真的有待批请求时显示）
        self._approval_row = tk.Frame(frame, bg=T.BG)
        self._approval_label = tk.Label(self._approval_row, text="", bg=T.BG, fg=T.WAIT,
                                        font=T.FONT_SMALL, anchor="w")
        self._approval_label.pack(side="left", padx=(0, 8))
        self._approve_btn = self._flat_button(self._approval_row, "通过",
                                             lambda: self._decide(True), fg=T.OK)
        self._approve_btn.pack(side="right", padx=(6, 0))
        self._reject_btn = self._flat_button(self._approval_row, "拒绝",
                                            lambda: self._decide(False), fg=T.DANGER)
        self._reject_btn.pack(side="right")

        self._set_tab(self.tab, save=False)
        self._bind_drag_and_click(*brand.widgets, state, elapsed)

    # -- Alert（概念图 03：贴附在窗口内的提示条） --------------------------

    def _build_alert(self, parent: Any) -> None:
        """高优先级提示条（贴附在窗口内）。

        与"工作态"拉开层级的三处硬手段（不是换个颜色就完事）：

        1. **独立结构**：左侧 4px 竖条 + 顶部 2px 强调线，与卡片本体分离；
        2. **三段信息**：`高优先级提示` 标题 / 具体问题 / 用户动作 + **热键胶囊**；
        3. **热键胶囊**：等宽粗体 + 边框 + 高对比底色，一眼能看到"按什么"。
        """
        tk = self.tk
        s = self._scale
        strip = tk.Frame(parent, bg=T.ALERT_STRIP_BG, height=self._alert_strip_height)
        self._alert = strip

        # 左侧竖条（危险/注意色，由 _update_alert 上色）
        self._alert_bar = tk.Frame(strip, bg=T.DANGER, width=T.px(4, s))
        self._alert_bar.pack(side="left", fill="y")

        inner = tk.Frame(strip, bg=T.ALERT_STRIP_BG)
        inner.pack(side="left", fill="both", expand=True,
                   padx=T.px(T.SPACE_M, s), pady=T.px(T.SPACE_XS, s))

        # 第 1 行：标题 + 关闭
        top = tk.Frame(inner, bg=T.ALERT_STRIP_BG)
        top.pack(fill="x")
        self._alert_title = tk.Label(top, text="高优先级提示", bg=T.ALERT_STRIP_BG,
                                     fg=T.DANGER, font=T.FONT_ALERT_TITLE, anchor="w")
        self._alert_title.pack(side="left")
        closer = tk.Label(top, text=T.ICONS["close"], bg=T.ALERT_STRIP_BG,
                          fg=T.ALERT_STRIP_TEXT, font=T.FONT_SMALL, cursor="hand2",
                          padx=T.px(T.SPACE_S, s))
        closer._icon_image = icon_image(tk, closer, "close", T.ALERT_STRIP_TEXT, s)
        closer.configure(image=closer._icon_image)
        closer.pack(side="right")
        closer.bind("<Button-1>", lambda e: self._dismiss_alert())

        # 第 2 行：具体问题（可换行）
        self._alert_text = tk.Label(inner, text="", bg=T.ALERT_STRIP_BG,
                                    fg=T.ALERT_STRIP_TEXT, font=T.FONT_SMALL,
                                    anchor="w", justify="left")
        self._alert_text.pack(fill="x")

        # 第 3 行：用户动作 + 热键胶囊
        action = tk.Frame(inner, bg=T.ALERT_STRIP_BG)
        action.pack(fill="x", pady=(T.px(T.SPACE_XS, s), 0))
        self._alert_action = tk.Label(action, text="请使用快捷键紧急停止",
                                      bg=T.ALERT_STRIP_BG, fg=T.ALERT_STRIP_TEXT,
                                      font=T.FONT_SMALL, anchor="w")
        self._alert_action.pack(side="left")
        self._alert_hotkey = tk.Label(action, text="", bg=T.SURFACE_2, fg=T.TEXT,
                                      font=T.FONT_HOTKEY,
                                      padx=T.px(T.SPACE_S, s), pady=0,
                                      highlightthickness=1,
                                      highlightbackground=T.DANGER,
                                      highlightcolor=T.DANGER)
        self._alert_hotkey.pack(side="left", padx=(T.px(T.SPACE_S, s), 0))

    def _alert_texts(self) -> str:
        """提示条上所有可见文字（测试与排障用；避免逐控件去猜）。"""
        parts: list[str] = []
        try:
            for widget in (self._alert_title, self._alert_text, self._alert_action,
                           self._alert_hotkey):
                parts.append(str(widget.cget("text")))
        except Exception:  # noqa: BLE001
            pass
        return " | ".join(p for p in parts if p)

    def _alert_content(self) -> tuple[int, str, str, str]:
        """从**真实快照**里挑出最需要提醒的一条。没有就返回 L0。

        返回 ``(level, 标题, 问题描述, 热键)``：标题里带"高优先级提示"字样，
        问题描述只讲**发生了什么**（不掺动作建议），动作与热键由 UI 固定呈现。
        """
        hotkey = "CTRL + ALT + F12"
        safety = self._safety_snapshot()
        if safety is not None:
            hotkey = self._hotkey_text(safety)
        if safety is not None and int(safety.attention.value) >= 5:
            title = ("高优先级提示：输入控制未释放" if safety.status is Status.RUNNING
                     else f"高优先级提示：{T.status_label(safety.status)}")
            message = (safety.activity.one_line if safety.activity else "") or "输入控制异常"
            return 5, title, message, hotkey
        ranked = self._ranked()
        if not ranked:
            return 0, "", "", hotkey
        top = ranked[0]
        level = int(top.attention.value)
        if level < 4:
            return level, "", "", hotkey
        title = f"高优先级提示：{top.agent.name} · {T.status_label(top.status)}"
        message = top.attention_reason or (top.activity.one_line if top.activity else "")
        if safety is not None and int(safety.attention.value) >= 5:
            level = 5
            title = ("高优先级提示：输入控制未释放" if safety.status is Status.RUNNING
                     else f"高优先级提示：{T.status_label(safety.status)}")
            message = (safety.activity.one_line if safety.activity else "") or "输入控制异常"
            message = message.split("·")[0].strip() or message
        if not message:
            message = "需要你处理"
        return level, title, message, hotkey

    def _update_alert(self) -> None:
        """Attention ≥ L4 → 在窗口内贴出提示条；L5（安全）优先。"""
        level, title, message, hotkey = self._alert_content()
        now = time.time()
        if level < 4 or (level < 5 and now < self._alert_dismissed_until):
            # 顺序很重要：先降 level 再重算窗口高度，否则 _resize_for_extra()
            # 仍按"要留出提示条"算，窗口不会回落（实测踩过）。
            self._alert_level = 0
            self._hide_alert()
            self._alert_state = (0, "")
            return
        if level != self._alert_level:
            self._alert_level = level
            self._layout_alert(visible=True)
        signature = (level, title + message)
        if self._alert_state == signature and self._alert.winfo_ismapped():
            return
        self._alert_state = signature
        danger = level >= 5
        accent = T.DANGER if danger else T.NOTICE
        bg = T.ALERT_STRIP_BG if danger else T.NOTICE_BG
        try:
            self._alert.configure(bg=bg)
            for child in self._alert.winfo_children():
                child.configure(bg=accent if child is self._alert_bar else bg)
                for sub in child.winfo_children():
                    sub.configure(bg=accent if sub is self._alert_bar else bg)
                    for leaf in sub.winfo_children():
                        leaf.configure(bg=bg)
            self._alert_title.configure(text=title or "高优先级提示", fg=accent)
            self._alert_text.configure(text=message)
            self._alert_action.configure(text="请使用快捷键紧急停止" if danger
                                         else "请查看当前任务与验证结果", fg=T.ALERT_STRIP_TEXT)
            if danger:
                self._alert_hotkey.pack(side="left", padx=(T.px(T.SPACE_S, self._scale), 0))
            else:
                self._alert_hotkey.pack_forget()
            self._alert_hotkey.configure(text=hotkey, fg=accent,
                                        highlightbackground=accent)
            width_px = max(260, self._scaled_size()[0] - T.px(64, self._scale))
            self._alert_title.configure(wraplength=width_px, justify="left")
            two_lines = len(message) > 34
            self._alert_two_lines = two_lines
            self._alert_text.configure(wraplength=width_px, justify="left")
            self._alert.configure(height=self._alert_strip_height)
            # 让 Tk 算一遍真实需求高度，再据此定窗口高度（否则会裁掉热键胶囊）
            self._alert.update_idletasks()
            self._alert_px_height = max(self._alert_strip_height,
                                        int(self._alert.winfo_reqheight()))
            self._layout_alert(visible=True)
        except Exception:  # noqa: BLE001
            pass

    def _layout_alert(self, *, visible: bool) -> None:
        """提示条显示/隐藏 → 同时调整窗口高度（贴附式，不是独立悬浮窗）。"""
        try:
            if visible:
                if not self._alert.winfo_ismapped():
                    self._alert.pack(side="bottom", fill="x")
            else:
                self._alert.pack_forget()
        except Exception:  # noqa: BLE001
            return
        self._resize_for_extra()

    def _hide_alert(self) -> None:
        """隐藏提示条并让窗口高度回落。

        ``was_visible`` 判定是必要的：``_update_alert`` 每个 tick 都会走到这里，
        若无条件重设几何，就会不停地动窗口（也会把拖拽位置弹回去）。
        """
        was_visible = bool(self._alert is not None and self._alert.winfo_ismapped())
        try:
            self._alert.pack_forget()
        except Exception:  # noqa: BLE001
            pass
        self._alert_px_height = 0
        if was_visible:
            self._resize_for_extra()

    def _resize_for_extra(self) -> None:
        root = self._root
        if root is None:
            return
        w, h = self._scaled_size()
        try:
            # **只给尺寸**：不要带 +x+y。带位置时会用"还未被事件循环更新的"
            # winfo_x/y 覆盖用户刚拖到的位置（实测：拖拽后窗口被每一 tick 弹回原处）。
            root.geometry(f"{w}x{h}")
        except Exception:  # noqa: BLE001
            pass
        self._redraw_surface()

    def _dismiss_alert(self) -> None:
        self._alert_dismissed_until = time.time() + 60.0
        self._alert_state = (0, "")
        self._alert_level = 0          # 先降 level（见 _update_alert 里的说明）
        self._hide_alert()

    # -- 小控件工厂 ---------------------------------------------------------

    def _flat_button(self, parent: Any, text: str, command: Any, *,
                     fg: str = T.TEXT_DIM) -> Any:
        tk = self.tk
        btn = tk.Label(parent, text=text, bg=T.SURFACE_2, fg=fg,
                       font=T.FONT_SMALL, padx=T.px(T.BTN_PAD_X, self._scale),
                       pady=T.px(T.BTN_PAD_Y, self._scale), cursor="hand2", takefocus=0)
        btn.bind("<Button-1>", lambda e: (command(), "break")[1])
        btn.bind("<Enter>", lambda e: btn.configure(bg=T.SELECTED_BG))
        btn.bind("<Leave>", lambda e: btn.configure(bg=T.SURFACE_2))
        btn.bind("<Button-3>", lambda e: self._popup_menu(e))
        return btn

    def _icon_button(self, parent: Any, glyph: str, command: Any, *, key: str,
                     danger: bool = False) -> Any:
        """图标按钮（概念图的关键操作簇）。悬停时在活动行显示**中文提示**，
        保证"图标排版 + 中文可懂"同时成立。"""
        tk = self.tk
        btn = tk.Label(parent, text=glyph, bg=T.SURFACE_2,
                       fg=T.DANGER if danger else T.TEXT_DIM, font=T.FONT_ICON,
                       padx=T.px(T.ICON_PAD_X, self._scale), pady=T.px(T.ICON_PAD_Y, self._scale),
                       cursor="hand2", takefocus=0)
        btn._icon_key = key
        btn._icon_image = icon_image(tk, btn, key, T.DANGER if danger else T.TEXT_DIM, self._scale)
        btn.configure(image=btn._icon_image)
        btn.bind("<Button-1>", lambda e: (command(), "break")[1])
        btn.bind("<Enter>", lambda e: (btn.configure(bg=T.SELECTED_BG),
                                       self._hint(T.ICON_HINTS.get(key, ""), seconds=2.5))[1])
        btn.bind("<Leave>", lambda e: btn.configure(bg=T.SURFACE_2))
        btn.bind("<Button-3>", lambda e: self._popup_menu(e))
        return btn

    def _kv_row(self, parent: Any, label: str) -> Any:
        tk = self.tk
        row = tk.Frame(parent, bg=T.BG)
        row.pack(fill="x", pady=int(1 * self._scale))
        tk.Label(row, text=label, bg=T.BG, fg=T.TEXT_MUTED,
                 font=T.FONT_TINY, width=8, anchor="w").pack(side="left")
        value = tk.Label(row, text="—", bg=T.BG, fg=T.TEXT, font=T.FONT_SMALL,
                         anchor="w", justify="left")
        value.pack(side="left", fill="x", expand=True)
        return value

    def _bind_drag_and_click(self, *widgets: Any, on_click: Any = None) -> None:
        for widget in widgets:
            widget.bind("<Button-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.bind("<ButtonRelease-1>", lambda e: self._save_settings())
            widget.bind("<Double-Button-1>", lambda e: self._apply_form("expanded"))
            if on_click is not None:
                widget.bind("<Button-1>", lambda e, cb=on_click: (self._drag_start(e), cb())[1])
            widget.bind("<Button-3>", lambda e: self._popup_menu(e))

    def _popup_menu(self, event: Any) -> None:
        tk = self.tk
        menu = tk.Menu(self._root, tearoff=False)
        for mode, label in (("mini", "最小化"), ("compact", "简洁模式"), ("expanded", "展开面板")):
            menu.add_command(label=label, command=lambda m=mode: self._apply_form(m))
        agents = tk.Menu(menu, tearoff=False)
        agents.add_command(label="自动选择", command=lambda: self._select_agent(None))
        for snap in self._ranked():
            agents.add_command(label=f"{snap.agent.name} ({snap.agent.id})",
                               command=lambda aid=snap.agent.id: self._select_agent(aid))
        menu.add_cascade(label="当前 Agent", menu=agents)
        menu.add_separator()
        menu.add_command(label="静音 / 取消静音", command=self.toggle_mute)
        menu.add_command(label="关闭", command=self.close)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # ==================================================================
    # 形态 / 分页 / 几何
    # ==================================================================

    def _set_tab(self, key: str, *, save: bool = True) -> None:
        if key not in self._tab_frames:
            return
        self.tab = key
        for name, frame in self._tab_frames.items():
            if name == key:
                frame.pack(fill="both", expand=True)
            else:
                frame.pack_forget()
        for name, btn in self._tab_buttons.items():
            try:
                btn.configure(bg=T.SELECTED_BG if name == key else T.SURFACE_3,
                              fg=T.TEXT if name == key else T.TEXT_DIM)
            except Exception:  # noqa: BLE001
                pass
        if save:
            self._save_settings()

    def _apply_form(self, mode: str, *, save: bool = True) -> None:
        if mode not in self.MODES:
            return
        self.mode = mode
        for name, frame in self._forms.items():
            if name == mode:
                frame.pack(side="top", fill="both", expand=True)
            else:
                frame.pack_forget()
        if self._alert_level >= 4:
            self._alert.pack(side="bottom", fill="x")
        else:
            self._alert.pack_forget()
        w, h = self._scaled_size()
        root = self._root
        try:
            if root.winfo_ismapped():
                self._sync_window_size()
            else:
                root.geometry(self._initial_geometry(w, h))
                self._last_size = (w, h)
        except Exception:  # noqa: BLE001
            pass
        root.minsize(w, h)
        self._redraw_surface()
        self._render()
        self._sync_window_size()
        if save:
            self._save_settings()
            self._no_activate()

    def _initial_geometry(self, w: int, h: int) -> str:
        root = self._root
        sx = int(self.settings.get("x", root.winfo_screenwidth() - w - 24))
        sy = int(self.settings.get("y", 110))
        sx, sy = clamp_position(sx, sy, w, h, work_areas(root))
        return f"{w}x{h}" + tk_position(sx, sy)

    def _drag_start(self, event: Any) -> None:
        self._drag_anchor = (event.x_root - self._root.winfo_x(),
                             event.y_root - self._root.winfo_y())

    def _drag_move(self, event: Any) -> None:
        dx, dy = self._drag_anchor
        root = self._root
        x, y = clamp_position(event.x_root-dx, event.y_root-dy,
                              root.winfo_width(), root.winfo_height(), work_areas(root))
        root.geometry(tk_position(x, y))

    def _save_settings(self) -> None:
        if self._root is None:
            return
        try:
            self.settings.update(mode=self.mode, x=self._root.winfo_x(),
                                 y=self._root.winfo_y(), selected=self.selected,
                                 tab=self.tab)
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.settings_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.settings), encoding="utf-8")
            tmp.replace(self.settings_path)
        except Exception:  # noqa: BLE001
            pass

    # ==================================================================
    # 数据接入
    # ==================================================================

    def _drain(self) -> None:
        self._prune_timers()
        dirty = False
        processed = 0
        while processed < 500:
            try:
                kind, payload = self._inbox.get_nowait()
            except Exception:  # queue.Empty
                break
            processed += 1
            if kind == "snapshot":
                self._snapshots[payload.agent.id] = payload
                self._changed.add(payload.agent.id)
                note = self.notifier.consider(payload)
                if note is not None:
                    dirty = True
            elif kind == "control_sent":
                self._control_message_until = time.monotonic() + 5
                action, result = payload
                self._control_busy = False
                if result.get("ok") and result.get("id"):
                    self._control_pending = {"id": result["id"], "deadline": time.monotonic()+8}
                else:
                    self._control_message = "发送失败：" + str(result.get("error", "Hub 未确认"))[:40]
                dirty = True
            elif kind == "control_result":
                self._control_message_until = time.monotonic() + 5
                self._control_busy = False
                if payload.get("state") in ("applied", "failed", "expired"):
                    self._control_pending = None
                    self._control_message = str(payload.get("message") or "请求已结束")
                dirty = True
            elif kind == "removed":
                self._changed.add(payload)
                self._snapshots.pop(payload, None)
                dirty = True
            elif kind == "status":
                self._stream_status = payload
                if payload == "connected":
                    self._snapshots.clear()
                dirty = True
        if dirty or self._changed:
            self._render()
        self._update_alert()
        if not self._stop.is_set():
            self._after_ids.append(self._root.after(self.poll_ms, self._drain))

    def _sync_cards(self) -> None:  # 基类钩子：v2 由 _render 统一接管
        self._render()

    def _tick(self) -> None:
        if self._stop.is_set():
            return
        self._render()
        self._update_alert()
        self._maybe_fetch_timeline()
        self._maybe_fetch_approvals()
        self._check_control()
        self._check_display()
        self._after_ids.append(self._root.after(self.clock_ms, self._tick))

    def _animate(self) -> None:
        if self._stop.is_set():
            return
        try:
            if self.mode != "mini" and self._root.winfo_viewable():
                snap = self._current()
                if progress_view(snap, self._stream_status == "connected").mode == "indeterminate":
                    self._advance_thumb()
        except Exception:  # noqa: BLE001
            pass
        self._after_ids.append(self._root.after(_ACTIVITY_STEP_MS, self._animate))

    def _advance_thumb(self) -> None:
        widgets = self._progress_widgets.get(self.mode)
        if not widgets:
            return
        track, thumb, label = widgets
        width = max(1, track.winfo_width())
        seg = max(24, int(width * 0.28))
        span = width + seg
        self._activity_phase = (self._activity_phase + max(4, int(width * 0.035))) % span
        x = self._activity_phase - seg
        left, right = max(0, x), min(width, x + seg)
        if right > left:
            thumb.place(x=left, y=0, width=right-left, relwidth=0, relheight=1)
        else:
            thumb.place_forget()

    def _render_progress(self, snap, connected):
        view = progress_view(snap, connected)
        if not self._control_pending and not self._control_busy and time.monotonic() > self._control_message_until:
            self._control_message = ""
        color = {"ok": T.OK, "danger": T.DANGER, "wait": T.WAIT,
                 "muted": T.MUTED, "active": T.ACCENT}.get(view.tone, T.IDLE)
        for track, thumb, label in self._progress_widgets.values():
            label.configure(text=self._control_message or view.text, fg=color)
            thumb.configure(bg=color)
            if view.mode == "determinate" and view.ratio:
                thumb.place(x=0, y=0, relwidth=view.ratio, relheight=1, width=0)
            elif view.mode != "indeterminate":
                thumb.place_forget()

    def _check_display(self):
        now = time.monotonic()
        if now < self._geometry_check_at:
            return
        self._geometry_check_at = now + 2
        root = self._root
        scale = dpi_scale(root)
        if abs(scale-self._scale) > 0.02:
            self._scale = scale
            configure_fonts(root, scale)
            for form in self._forms.values():
                for child in form.winfo_children():
                    child.destroy()
            self._alert.destroy()
            for name in ("_brands", "_dots", "_state_labels", "_activity_labels", "_elapsed_labels",
                         "_detail_widgets", "_method_widgets", "_settings_widgets", "_timeline_widgets",
                         "_tab_buttons", "_tab_frames", "_progress_widgets"):
                getattr(self, name).clear()
            self._pause_buttons.clear(); self._stop_buttons.clear(); self._stamps.clear()
            self._build_mini(self._forms["mini"])
            self._build_compact(self._forms["compact"])
            self._build_expanded(self._forms["expanded"])
            self._build_alert(self._surface)
            self._alert_px_height = 0
            self._alert_state = (0, "")
            self._last_size = (0, 0)
            self._apply_form(self.mode, save=False)
            self._update_alert()
        x, y = root.winfo_x(), root.winfo_y()
        nx, ny = clamp_position(x, y, root.winfo_width(), root.winfo_height(), work_areas(root))
        if (x, y) != (nx, ny):
            root.geometry(tk_position(nx, ny))

    def _maybe_fetch_timeline(self) -> None:
        if self.mode != "expanded" or self.tab != "task" or self._timeline_busy:
            return
        now = time.time()
        if now - self._timeline_at < 2.0:
            return
        self._timeline_at = now
        self._timeline_busy = True
        agent = self._current_agent_id()

        def run() -> None:
            try:
                self._timeline_rows = self.client.timeline(agent, limit=8, timeout=1.2) or []
            except Exception:  # noqa: BLE001
                self._timeline_rows = []
            finally:
                self._timeline_busy = False

        threading.Thread(target=run, name="uah-hud-timeline", daemon=True).start()

    def _maybe_fetch_approvals(self) -> None:
        """等批准时才有意义：2 秒一次后台拉取 pending（失败静默）。"""
        if self._approval_busy:
            return
        snap = self._current()
        interested = (self.mode == "expanded"
                      or (snap is not None and snap.status in (Status.WAITING_APPROVAL,
                                                               Status.WAITING_INPUT)))
        if not interested:
            self._approval_pending = []
            return
        now = time.time()
        if now < self._approval_next:
            return
        self._approval_next = now + 2.0
        self._approval_busy = True

        def run() -> None:
            try:
                from ...core.approval import approval_call

                result = approval_call(self.url, "pending", {}, token=self._read_token())
                self._approval_pending = list(result.get("pending") or [])
            except Exception:  # noqa: BLE001
                self._approval_pending = []
            finally:
                self._approval_busy = False

        threading.Thread(target=run, name="uah-hud-approval", daemon=True).start()

    def _decide(self, allow: bool) -> None:
        request = getattr(self, "_current_approval", None)
        if not request:
            return
        self._current_approval = None
        try:
            self._approval_row.pack_forget()
        except Exception:  # noqa: BLE001
            pass

        def send() -> None:
            try:
                from ...core.approval import approval_call

                approval_call(self.url, "decide",
                              {"req_id": request["req_id"], "allow": bool(allow)},
                              token=self._read_token())
            except Exception:  # noqa: BLE001 - Agent 按超时/断线拒绝（fail-closed）
                pass

        threading.Thread(target=send, name="uah-hud-approval-decide", daemon=True).start()
        self._hint("已通过" if allow else "已拒绝")

    # ==================================================================
    # 渲染
    # ==================================================================

    def _current_agent_id(self) -> str | None:
        if self.selected != SAFETY_AGENT_ID and self.selected in self._snapshots:
            return self.selected
        ranked = self._ranked()
        return ranked[0].agent.id if ranked else None

    def _select_agent(self, agent_id):
        self.selected = agent_id
        self._timeline_rows = []
        self._timeline_at = 0
        self._render()
        self._save_settings()

    def _current(self) -> Any:
        aid = self._current_agent_id()
        return self._snapshots.get(aid) if aid else None

    def _ranked(self) -> list[Any]:
        """排序：Attention 高的优先，其次最近更新。与状态色无关。"""
        return sorted((s for s in self._snapshots.values() if s.agent.id != SAFETY_AGENT_ID),
                      key=lambda s: (-int(s.attention.value), -s.updated_at))

    def _safety_snapshot(self) -> Any:
        return self._snapshots.get(SAFETY_AGENT_ID)

    def _render(self) -> None:
        if not self._forms:
            return
        snap = self._current()
        safety = self._safety_snapshot()
        disconnected = self._stream_status != "connected"
        for mode in self.MODES:
            self._render_common(mode, snap, disconnected)
        self._render_details(snap)
        self._render_progress(snap, not disconnected)
        self._render_method(snap, safety)
        # 内容变化（审批行出现 / 状态行变长）会改变需求高度：只在尺寸真的变了时才动几何
        self._sync_window_size()

    def _render_common(self, mode: str, snap: Any, disconnected: bool) -> None:
        brand = self._brands.get(mode)
        state = self._state_labels.get(mode)
        activity = self._activity_labels.get(mode)
        elapsed = self._elapsed_labels.get(mode)

        if snap is None:
            if brand is not None:
                brand.update("IDLE", 0, disconnected=disconnected)
            if state is not None:
                state.configure(text="未连接 Hub" if disconnected else "等待 Agent",
                                fg=T.TEXT_DIM)
            if activity is not None:
                activity.configure(text=self._stream_status if disconnected else "")
            if elapsed is not None:
                elapsed.configure(text="")
            return

        status = snap.status
        alive = not (snap.stale or disconnected)
        color = T.MUTED if not alive else T.status_color(status)
        if brand is not None:
            brand.update(status.value, int(snap.attention.value),
                         disconnected=disconnected, color=color)

        # 概念图 02/04 的口径是「状态 · 当前环境」：优先项目/环境名，其次执行方式
        env = snap.project.name or T.method_label(snap.activity.tool) or ""
        label = T.status_label(status)
        if snap.stale:
            label, env = f"离线 {snap.stale_for_s:.0f}s", ""
        elif disconnected:
            label, env = "未连接 Hub", ""
        if state is not None:
            state.configure(text=f"{label} · {env}" if env else label,
                            fg=T.TEXT if alive else T.TEXT_DIM)
        if activity is not None:
            text = snap.activity.one_line or snap.task.stage or snap.task.name or ""
            activity.configure(text=text[:46])
        if elapsed is not None:
            elapsed.configure(text=self._elapsed_text(snap))

    @staticmethod
    def _elapsed_text(snap: Any) -> str:
        return task_elapsed(snap)

    @staticmethod
    def _put(widget: Any, text: str | None, color: str = T.TEXT) -> None:
        if widget is None:
            return
        try:
            widget.configure(text=text or "—", fg=color if text else T.TEXT_MUTED)
        except Exception:  # noqa: BLE001
            pass

    def _render_details(self, snap: Any) -> None:
        if not self._detail_widgets:
            return
        if snap is None:
            for key in ("task", "action", "step"):
                self._put(self._detail_widgets.get(key), None)
        else:
            task_name = snap.task.name or "（未命名任务）"
            self._put(self._detail_widgets.get("task"),
                      f"{snap.task.phase} · {task_name}" if snap.task.phase else task_name)
            self._put(self._detail_widgets.get("action"),
                      snap.activity.summary or snap.activity.detail or snap.task.stage)
            step = snap.task.progress_text
            self._put(self._detail_widgets.get("step"),
                      f"{step}（{snap.task.stage}）" if step and snap.task.stage
                      else (step or snap.task.stage))

        label = self._timeline_widgets.get("label")
        if label is not None:
            rows = [r for r in self._timeline_rows if r.get("text")][:5]
            if not rows:
                label.configure(text="（暂无事件）", fg=T.TEXT_MUTED)
            else:
                lines = []
                for row in rows:
                    ts = time.strftime("%H:%M:%S", time.localtime(float(row.get("ts") or 0)))
                    mark = "●" if int(row.get("attention") or 0) >= 3 else "·"
                    lines.append(f"{ts}  {mark} {str(row.get('text') or '')[:34]}")
                label.configure(text="\n".join(lines), fg=T.TEXT_DIM)

        paused = bool(snap is not None and (snap.status is Status.PAUSED or
                      snap.runtime.get("control_command") == "PAUSE"))
        self._paused_locally = paused
        for btn in self._pause_buttons:
            try:
                if btn.cget("text") in (T.ICONS["pause"], T.ICONS["resume"]):
                    key = "resume" if paused else "pause"
                    btn.configure(text=T.ICONS[key])
                    if getattr(btn, "_icon_key", None) != key:
                        btn._icon_key = key
                        btn._icon_image = icon_image(self.tk, btn, key, T.TEXT_DIM, self._scale)
                        btn.configure(image=btn._icon_image)
                else:
                    btn.configure(text="继续" if paused else "暂停")
            except Exception:  # noqa: BLE001
                pass

        pending = [p for p in self._approval_pending if p.get("req_id")]
        if snap is not None and pending:
            mine = [p for p in pending if str(p.get("agent_id") or "") == snap.agent.id]
            pending = mine or pending
        self._current_approval = pending[0] if pending else None
        try:
            if self._current_approval is not None:
                summary = str(self._current_approval.get("summary") or "等待审批")
                self._approval_label.configure(text=f"待审批：{summary[:26]}")
                if not self._approval_row.winfo_ismapped():
                    self._approval_row.pack(side="bottom", fill="x",
                                            padx=T.px(T.SPACE_M, self._scale),
                                            pady=(T.px(T.SPACE_XS, self._scale), 0))
            else:
                self._approval_row.pack_forget()
        except Exception:  # noqa: BLE001
            pass

    def _render_method(self, snap: Any, safety: Any) -> None:
        if not self._method_widgets:
            return
        if snap is None:
            for key in ("method", "phase", "attention"):
                self._put(self._method_widgets.get(key), None)
        else:
            self._put(self._method_widgets.get("method"),
                      T.method_label(snap.activity.tool) or "等待选路")
            self._put(self._method_widgets.get("phase"),
                      " · ".join(b for b in (snap.task.phase, snap.task.stage) if b))
            att = snap.attention
            reason = f"（{snap.attention_reason}）" if snap.attention_reason else ""
            self._put(self._method_widgets.get("attention"),
                      f"{att.name} {att.label}{reason}" if int(att.value) > 0
                      else "L0 无需关注",
                      T.attention_color(int(att.value)))
        if self._safety_label is not None:
            if safety is None:
                self._put(self._safety_label, "安全层未连接（HUD 不依赖它）", T.TEXT_MUTED)
            else:
                text = (safety.activity.one_line if safety.activity else "") or ""
                hotkey = self._hotkey_text(safety)
                self._put(self._safety_label, f"{text} · 急停 {hotkey}".strip(" ·"),
                          T.attention_color(int(safety.attention.value)))
        meta = snap.runtime if snap else {}
        self._put(self._settings_widgets.get("domain"), meta.get("task_domain") or "未上报")
        self._put(self._settings_widgets.get("execution"),
                  ("仅预演" if meta["dry_run"] else "实际执行") if "dry_run" in meta else "未上报")
        permissions = []
        for key, label in (("never_delete_assets", "禁止删除资产"),
                           ("never_modify_core_config", "禁止修改核心配置")):
            if key in meta:
                permissions.append(label + ("：开" if meta[key] else "：关"))
        if "max_actors_per_batch" in meta:
            permissions.append(f"批量上限：{meta['max_actors_per_batch']}")
        self._put(self._settings_widgets.get("permissions"), "；".join(permissions) or "未上报")
        self._put(self._settings_widgets.get("approval"), meta.get("approval") or "未上报")
        hk = self._settings_widgets.get("hotkey")
        if hk is not None:
            self._put(hk, self._hotkey_text(safety) if safety else (
                str(meta.get("hotkey", "未上报")).upper() +
                ("（已启用）" if meta.get("hotkey_active") else "（未确认启用）")),
                      T.TEXT_DIM)

    @staticmethod
    def _hotkey_text(safety: Any) -> str:
        """急停热键**取自真实安全状态**（safety 的 activity.detail 里带 hotkey=…）。"""
        detail = str(getattr(getattr(safety, "activity", None), "detail", "") or "")
        hotkey = ""
        for part in detail.split("·"):
            part = part.strip()
            if part.lower().startswith("hotkey="):
                hotkey = part.split("=", 1)[1].strip()
                break
        if not hotkey:
            hotkey = "ctrl+alt+f12"
        return hotkey.replace("+", " + ").upper()

    def _update_conn(self) -> None:  # 兼容基类调用
        return None

    # ==================================================================
    # 控制（软控制；急停不在 HUD）
    # ==================================================================

    def _control(self, action: str) -> None:
        if action not in ("pause", "resume", "stop"):
            return
        snap = self._current()
        self._control_message_until = time.monotonic()+5
        if snap is None or snap.stale or snap.stopped or self._stream_status != "connected":
            self._control_message = "无法控制：Agent 未连接"
            self._render()
            return
        if not snap.runtime.get("soft_control"):
            self._control_message = "该 Agent 未声明软控制能力"
            self._render()
            return
        if self._control_pending or self._control_busy:
            return
        self._control_busy = True
        self._control_message = {"pause": "正在请求暂停…", "resume": "正在请求继续…",
                                 "stop": "正在请求停止…"}[action]
        self._render()
        def send():
            try:
                result = self.client.submit_control(action, agent_id=snap.agent.id, token=self._read_token())
            except Exception as exc:
                result = {"ok": False, "error": str(exc)[:120]}
            self._inbox.put(("control_sent", (action, result)))
        threading.Thread(target=send, name="uah-control-send", daemon=True).start()

    def _check_control(self):
        pending = self._control_pending
        if not pending or self._control_busy:
            return
        if time.monotonic() >= pending["deadline"]:
            self._control_pending = None
            self._control_message = "控制请求超时，未确认生效"
            self._control_message_until = time.monotonic()+5
            return
        self._control_busy = True
        def check():
            try:
                result = self.client._get("/control/result?id=" + pending["id"], timeout=1)
                if not isinstance(result, dict):
                    result = {}
            except Exception:
                result = {}
            self._inbox.put(("control_result", result))
        threading.Thread(target=check, name="uah-control-result", daemon=True).start()

    def _read_token(self) -> str | None:
        try:
            from ...core.approval import token_path

            self._control_token = token_path(self.url).read_text(encoding="utf-8").strip()
        except Exception:  # noqa: BLE001
            self._control_token = None
        return self._control_token

    def _toggle_pause(self) -> None:
        self._control("resume" if self._paused_locally else "pause")

    def _hint(self, text: str, *, seconds: float = 6.0) -> None:
        if not text:
            return
        label = self._hint_label
        if label is None:
            state = self._state_labels.get(self.mode)
            if state is not None:
                state.configure(text=text, fg=T.WAIT)
            return
        label.configure(text=text)
        self._after_ids.append(self._root.after(int(seconds * 1000),
            lambda: label.configure(text="") if label.winfo_exists() else None))

    # ==================================================================
    # 提醒 / 生命周期
    # ==================================================================

    def _on_notify(self, note: Any) -> None:
        """提醒：只做轻量强调（不抬窗、不抢焦点——那是安全横幅的活）。"""
        self._flash_until = time.time() + T.FLASH_SECONDS
        self._render()

    def close(self) -> None:
        self._save_settings()
        super().close()


__all__ = ["HudApp", "enable_dpi_awareness", "SAFETY_AGENT_ID", "TABS"]
