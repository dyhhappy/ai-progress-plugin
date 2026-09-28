"""OC 印章 —— 品牌印章，不是桌宠。

定位与硬约束：

* OC 是**额外添加**的印章，**绝不替换**状态灯，也**绝不替换** UAH 文字标识；
* 尺寸克制（默认 22 逻辑像素），像一枚趴在 UAH 旁边的小印章；
* **不重画角色**：只从 ``uah/theme/assets/oc_stamp[_状态].png`` 加载官方素材；
  素材缺失时渲染一个中性占位章（冷蓝描边 + 「UAH」字母），而不是自造一个萌妹。
* 状态表达的主责在状态灯/文字/Attention，OC 最多做**极轻微**变化
  （这里只做透明度/描边色的轻微差异，不重绘角色）。

实现上刻意只用 tkinter 原生能力：``tk.PhotoImage`` 支持 PNG（Tk 8.6+），
不引入 Pillow 依赖，与「uah 只用 stdlib」的约束一致。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..theme import tokens as T

ASSETS_DIR = Path(__file__).resolve().parents[1] / "theme" / "oc"

#: 主题层的状态槽 → 素材名（由 uah/theme/oc/build_stamps.py 从 OC 原始设定图裁切生成）。
#: 缺文件时统一回落到 ``oc_stamp.png``（标准印章）。
#: 品牌区用小尺寸，所以默认用**半身像**（``oc_bust_*``）：28 逻辑像素下仍能
#: 辨认银白偏冷蓝头发 / 异色瞳 / 困困半睁眼 / 黑白蓝服装轮廓。
#: 全身姿态（``oc_stamp_*``）保留给较大展示场景，由 ``kind`` 参数切换。
_STATE_ASSETS: dict[str, str] = {
    "idle": "oc_bust.png",
    "starting": "oc_bust_starting.png",
    "working": "oc_bust_working.png",
    "thinking": "oc_bust_thinking.png",
    "waiting": "oc_bust_waiting.png",
    "warning": "oc_bust_warning.png",
    "completed": "oc_bust_completed.png",
    "paused": "oc_bust_paused.png",
}

#: 全身姿态（同样由 build_stamps.py 生成，需要更大展示时用）
_STATE_ASSETS_FULL: dict[str, str] = {
    k: v.replace("oc_bust", "oc_stamp") for k, v in _STATE_ASSETS.items()
}

_warned = False


def state_key_for(status_value: str, attention: int = 0) -> str:
    """Status/Attention → 印章状态槽。纯函数，便于测试。"""
    s = str(status_value or "").upper()
    if int(attention or 0) >= 4 or s in ("ERROR", "BLOCKED"):
        return "warning"
    if s in ("WAITING_INPUT", "WAITING_APPROVAL"):
        return "waiting"
    if s == "DONE":
        return "completed"
    if s in ("PAUSED", "CANCELLED"):
        return "paused"
    if s in ("STARTING", "RETRYING"):
        return "starting"
    if s == "RUNNING":
        return "working"
    return "idle"


#: 预生成档位（与 uah/theme/oc/build_stamps.py 的 SIZES 对应）
STAMP_SIZES: tuple[int, ...] = (48, 72, 96, 128, 176, 256)


def _stamp_path(name: str, size: int) -> Path:
    """``oc_stamp_working.png`` + 96 → ``oc_stamp_working_96.png``；
    默认档（128）则是 ``oc_stamp_working.png``。"""
    if size == 128:
        return ASSETS_DIR / name
    return ASSETS_DIR / name.replace(".png", f"_{size}.png")


def resolve_asset(state_key: str, target_px: int = 0, *, kind: str = "bust") -> Path | None:
    """找到可用的印章图片；找不到返回 None（调用方走占位）。

    ``target_px`` 是**物理像素**目标。策略：挑"最接近且不小于目标"的一档，
    绝不拿小图去放大；由调用方再做一次精确缩放（有 Pillow 用 LANCZOS，
    没有就用整数子采样，至少不会变大）。
    """
    name = (_STATE_ASSETS if kind != "full" else _STATE_ASSETS_FULL).get(state_key)
    if not name:
        return None
    wanted = max(1, int(target_px or 0))
    ordered = sorted(STAMP_SIZES, key=lambda s: (s < wanted, abs(s - wanted)))
    for size in ordered:
        candidates = [_stamp_path(name, size)]
        if size == 128:
            candidates.append(ASSETS_DIR / name)          # 同文件，容错
        for path in candidates:
            try:
                if path.is_file() and path.stat().st_size > 0:
                    return path
            except OSError:
                continue
    fallback = ASSETS_DIR / "oc_stamp.png"
    try:
        if fallback.is_file():
            return fallback
    except OSError:
        pass
    return None


class OcStamp:
    """一枚印章控件。``widget`` 是可直接 pack 的 tkinter 控件。"""

    def __init__(self, parent: Any, tk: Any, *, size: int = 22, bg: str = T.SURFACE) -> None:
        self.tk = tk
        self.size = int(size)
        self.bg = bg
        self._image: Any = None
        self._state = ""
        self._available = False
        # Canvas：既能贴图片，也能在缺素材时画占位章
        self.widget = tk.Canvas(parent, width=self.size, height=self.size, bg=bg,
                                highlightthickness=0, bd=0)
        self._draw()

    # -- 内部 ---------------------------------------------------------------

    def _load_image(self, state_key: str) -> bool:
        path = resolve_asset(state_key, self.size)
        if path is None:
            return False
        try:
            # 必须显式 master=self.widget：不指定时 tkinter 会挂到"默认 root"上，
            # 而同一进程里重建 HUD（例如验收项"HUD 重启"）时默认 root 可能是另一个
            # Tk 实例 —— 图片会落在错误的解释器里，create_image 报
            # `image "pyimageNN" doesn't exist`。实测踩过。
            img = self.tk.PhotoImage(file=str(path), master=self.widget)
        except Exception:  # noqa: BLE001 - Tk 不支持该 PNG 变体 / 文件损坏
            return False
        # 缩放到目标物理尺寸。两条路径，**都不允许放大**：
        #   ① 有 Pillow → LANCZOS 精确缩放到目标像素（最清晰，HUD 跑在系统 3.12 时可用）；
        #   ② 没有 Pillow → tk 的整数子采样（会略糊但不会放大失真）。
        self._image = self._fit(img, path)
        return True

    def _fit(self, img: Any, path: Path) -> Any:
        """把加载到的图缩到 ``self.size`` 物理像素。

        两条路径，**都不允许放大**（宁可小一点也不糊）：

        ① 有 Pillow → LANCZOS 精确缩放到目标像素，再以 base64 PNG 交给 tk；
        ② 没有 Pillow（或转换失败）→ tk 原生整数子采样。
        """
        try:
            target = max(1, int(self.size))
            source = max(1, int(img.width()))
            if source <= target:
                return img                       # 源比目标小：不放大
            try:
                import base64
                import io as _io

                from PIL import Image as _PILImage            # 可选依赖：只影响清晰度

                with _PILImage.open(str(path)) as raw:
                    resized = raw.convert("RGBA").resize((target, target),
                                                         _PILImage.LANCZOS)
                    buf = _io.BytesIO()
                    resized.save(buf, format="PNG")
                encoded = base64.b64encode(buf.getvalue()).decode("ascii")
                return self.tk.PhotoImage(data=encoded, master=self.widget)
            except Exception:  # noqa: BLE001 - 无 Pillow / 转换失败 → 整数子采样
                factor = max(1, round(source / target))
                return img.subsample(factor, factor) if factor > 1 else img
        except Exception:  # noqa: BLE001
            return img

    def _draw_placeholder(self) -> None:
        """中性占位章：圆角方章 + 「OC」标记。

        刻意**不画人脸**：这是一枚"待替换的资源槽"，而不是一个自造的角色。
        文案用 OC 而不是 UAH，避免和旁边真正的 UAH 标识重复造成误解。
        """
        c = self.widget
        c.delete("all")
        s = self.size
        pad = 1
        c.create_rectangle(pad, pad, s - pad, s - pad, outline=T.BORDER_STRONG,
                           fill=T.SURFACE_2, width=1)
        c.create_text(s / 2, s / 2, text="OC", fill=T.TEXT_MUTED,
                      font=(T.FONT_FAMILY, max(5, int(s / 5)), "bold"))

    def _draw(self) -> None:
        c = self.widget
        c.delete("all")
        if self._image is not None:
            c.create_image(self.size / 2, self.size / 2, image=self._image)
            c.create_rectangle(0, 0, self.size - 1, self.size - 1,
                               outline=T.BORDER_STRONG, width=0)
            self._available = True
        else:
            self._draw_placeholder()
            self._available = False

    # -- 对外 ---------------------------------------------------------------

    @property
    def available(self) -> bool:
        """True = 用的是真实 OC 素材；False = 中性占位。"""
        return self._available

    @property
    def state_key(self) -> str:
        return self._state

    def update(self, status_value: str, attention: int = 0) -> None:
        key = state_key_for(status_value, attention)
        if key == self._state and self._image is not None:
            return
        self._state = key
        got = self._load_image(key)
        if not got:
            self._image = None
            global _warned
            if not _warned:
                _warned = True
                print(f"[uah] OC 印章素材缺失，使用中性占位（在 OC 原始设定图上跑 "
                      f"`python uah/theme/oc/build_stamps.py` 即可生成到 {ASSETS_DIR}）",
                      flush=True)
        self._draw()
        # 轻微状态差异：只调描边色，不改角色
        try:
            self.widget.configure(bg=self.bg)
        except Exception:  # noqa: BLE001
            pass

    def destroy(self) -> None:
        try:
            self.widget.destroy()
        except Exception:  # noqa: BLE001
            pass


__all__ = ["OcStamp", "state_key_for", "resolve_asset", "ASSETS_DIR", "STAMP_SIZES"]
