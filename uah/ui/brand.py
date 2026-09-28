"""品牌区 —— **所有 UI 形态共用同一套构成**。

固定构成（UI 概念图 01/02/03/05/06 一致）：

    [状态灯] UAH [OC 小印章] | 状态内容

硬约束：

* 状态灯与 ``UAH`` 字样是**主体**，任何情况下都不被 OC 取代；
* OC 是**附加印章**，缺素材时是中性占位章，绝不重画角色；
* Mini / Compact / Expanded / Alert 都用这一个类，不允许各画各的
  （否则品牌语言立刻漂移）。

用法：

    brand = BrandBar(parent, tk, scale=s, bg=T.BG)
    brand.frame.pack(side="left")
    brand.update(status_value="RUNNING", attention=0)   # 宿主渲染时调用
"""

from __future__ import annotations

from typing import Any

from ..theme import tokens as T
from .oc import OcStamp
from .icons import icon_image


class BrandBar:
    """``[状态灯] UAH [OC]``。尾部分隔符（``|``）由宿主自行追加。"""

    def __init__(
        self,
        parent: Any,
        tk: Any,
        *,
        scale: float = 1.0,
        bg: str = T.BG,
        stamp_size: int | None = None,
        show_stamp: bool = True,
    ) -> None:
        self.tk = tk
        self.bg = bg
        self.scale = float(scale)
        self.frame = tk.Frame(parent, bg=bg)

        dot_size = max(8, int(round(T.BRAND_DOT_SIZE * self.scale)))
        self.dot = tk.Label(self.frame, text=T.ICONS["dot_idle"], bg=bg, fg=T.IDLE,
                            font=(T.FONT_FAMILY, dot_size))
        self.dot.pack(side="left")
        self._dot_key = None

        self.name = tk.Label(self.frame, text="UAH", bg=bg, fg=T.TEXT, font=T.FONT_TITLE)
        self.name.pack(side="left", padx=(int(T.BRAND_GAP * self.scale), 0))

        self.stamp: OcStamp | None = None
        if show_stamp:
            size = int(stamp_size or T.BRAND_STAMP_SIZE * self.scale)
            self.stamp = OcStamp(self.frame, tk, size=size, bg=bg)
            self.stamp.widget.pack(side="left", padx=(int(T.BRAND_GAP * self.scale), 0))
            self.stamp.update("IDLE", 0)

        self.gap = int(T.BRAND_GAP * self.scale)

    # -- 对外 ---------------------------------------------------------------

    @property
    def widgets(self) -> tuple[Any, ...]:
        """可绑定拖拽/右键的控件集合。"""
        items = [self.frame, self.dot, self.name]
        if self.stamp is not None:
            items.append(self.stamp.widget)
        return tuple(items)

    @property
    def stamp_available(self) -> bool:
        """True = 用的是真实 OC 素材（非中性占位）。"""
        return bool(self.stamp and self.stamp.available)

    def update(self, status_value: str, attention: int = 0, *, disconnected: bool = False,
               color: str | None = None) -> None:
        """由宿主渲染时调用：状态灯 + OC 印章一起跟随状态。"""
        from ..core.models import Status, parse_status

        status = parse_status(status_value, default=Status.UNKNOWN)
        dot_color = color or T.status_color(status)
        if disconnected:
            dot_color = T.MUTED
        glyph = T.status_glyph(status)
        # 状态灯只用两种几何形（概念图只有实心/空心点），其余语义交给文字与颜色
        glyph = T.ICONS["dot_idle"] if glyph in ("○", "◌") else T.ICONS["dot_active"]
        try:
            key = ("ring" if disconnected or status in (Status.IDLE, Status.CANCELLED) else "dot", dot_color)
            if key != self._dot_key:
                self._dot_key = key
                self._dot_image = icon_image(self.tk, self.dot, key[0], dot_color, self.scale)
                self.dot.configure(text=glyph, fg=dot_color, image=self._dot_image)
        except Exception:  # noqa: BLE001
            pass
        if self.stamp is not None:
            self.stamp.update(status_value, attention)


__all__ = ["BrandBar"]
