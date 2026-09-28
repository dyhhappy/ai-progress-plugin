"""DPI / 缩放 —— 全部尺寸换算集中在这里（宿主不再各算各的）。

三条规则：

1. **逻辑像素 → 物理像素** 只经过 ``theme.tokens.px``；
2. **缩放系数** 只经过 :func:`dpi_scale`；
3. **DPI 感知** 只经过 :func:`enable_dpi_awareness`，且必须在创建任何窗口**之前**调用。

实测过的坑（2026-09-22）：只读 ``winfo_fpixels('1i')`` 不够 —— 某些启动方式下
（例如被其它进程以分离方式拉起）Tk 会报 96，而显示器其实是 150%，结果 HUD 只按
1.0 倍渲染，看起来比设计尺寸小一圈。所以这里按"窗口 → 系统 → Tk"三级取 DPI，
并在开启感知后**回读**实际生效方式，便于日志核对。

``UAH_HUD_SCALE`` 环境变量可**强制**缩放系数（例如 ``UAH_HUD_SCALE=1.25``），
用于在 100% / 125% / 150% 下做真实验证，不需要真的改系统缩放。
"""

from __future__ import annotations

import os

#: 强制缩放系数的环境变量（测试/排障用）。取值如 ``1``、``1.25``、``1.5``。
SCALE_ENV = "UAH_HUD_SCALE"

#: 允许的缩放区间（超出说明 DPI 探测异常，夹住以免窗口变成巨物或蚂蚁）
MIN_SCALE = 1.0
MAX_SCALE = 2.0

#: SetProcessDpiAwarenessContext 的取值（Win10 1703+）
_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4

#: 可选诊断：设置 ``UAH_DPI_DEBUG=<日志路径>`` 后把每次 DPI 判定写进该文件。
#: DPI 出问题时（窗口比预期小一圈）光看界面是查不出来的，所以留这个开关。
DEBUG_ENV = "UAH_DPI_DEBUG"


def _debug(msg: str) -> None:
    path = os.environ.get(DEBUG_ENV)
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{time_stamp()} {msg}\n")
    except OSError:
        pass


def time_stamp() -> str:
    import time as _t

    return _t.strftime("%H:%M:%S")


def _clamp(value: float) -> float:
    return max(MIN_SCALE, min(MAX_SCALE, float(value)))


def env_scale() -> float | None:
    """读取强制缩放系数；没设或非法返回 None。"""
    raw = os.environ.get(SCALE_ENV)
    if not raw:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return _clamp(value)


def _user32():
    import ctypes
    from ctypes import wintypes as w

    u = ctypes.WinDLL("user32", use_last_error=True)
    try:
        u.GetDpiForWindow.argtypes = [w.HWND]
        u.GetDpiForWindow.restype = ctypes.c_uint
        u.GetDpiForSystem.restype = ctypes.c_uint
    except Exception:  # noqa: BLE001
        pass
    return u


def window_dpi(root: object | None = None) -> int:
    """窗口所在显示器的 DPI（拿不到返回 0）。"""
    if os.name != "nt":
        return 0
    try:
        u = _user32()
        hwnd = int(root.winfo_id()) if root is not None else 0  # type: ignore[attr-defined]
        if hwnd:
            got = int(u.GetDpiForWindow(hwnd) or 0)
            if got > 0:
                return got
        return int(u.GetDpiForSystem() or 0)
    except Exception:  # noqa: BLE001
        return 0


def dpi_scale(root: object | None = None) -> float:
    """缩放系数：环境变量 → 窗口 DPI → 系统 DPI → Tk 自报 → 1.0。"""
    forced = env_scale()
    if forced is not None:
        return forced
    dpi = window_dpi(root)
    if dpi > 0:
        scale = _clamp(dpi / 96.0)
        _debug(f"dpi_scale: 取自 Win32 API dpi={dpi} -> scale={scale}")
        return scale
    try:
        pixels_per_inch = float(root.winfo_fpixels("1i"))  # type: ignore[attr-defined]
        scale = _clamp(pixels_per_inch / 96.0)
        _debug(f"dpi_scale: Win32 不可用，退回 Tk ppi={pixels_per_inch:.1f} -> scale={scale}")
        return scale
    except Exception:  # noqa: BLE001
        _debug("dpi_scale: 全部失败 -> 1.0")
        return 1.0


def dpi_report(root: object | None = None) -> dict[str, object]:
    """诊断用：把"用了哪级 DPI、算出多少"一次性说清（日志/测试断言都看它）。"""
    forced = env_scale()
    dpi = window_dpi(root)
    tk_ppi = None
    try:
        tk_ppi = float(root.winfo_fpixels("1i")) if root is not None else None  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        tk_ppi = None
    return {
        "env_scale": forced,
        "window_or_system_dpi": dpi,
        "tk_ppi": tk_ppi,
        "scale": dpi_scale(root),
        "awareness": _AWARENESS[0],
    }


#: 最近一次 enable_dpi_awareness 的实际结果（诊断用）
_AWARENESS: list[str] = ["unset"]


def enable_dpi_awareness() -> str:
    """开启 DPI 感知，返回**实际生效**的方式（便于日志核对）。

    依次尝试：``SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)`` →
    ``SetProcessDpiAwareness(2)`` → ``SetProcessDpiAwareness(1)`` → ``SetProcessDPIAware()``。
    必须在创建任何窗口之前调用；全部失败也不报错（退化为 unaware，只是会小一圈）。
    """
    if os.name != "nt":
        _AWARENESS[0] = "non-windows"
        return "non-windows"
    import ctypes

    result = "none"
    try:
        u = ctypes.WinDLL("user32", use_last_error=True)
    except Exception:  # noqa: BLE001
        u = None
    _debug(f"enable_dpi_awareness: 开始（us er32={'ok' if u else 'nil'}）")
    # 1) PER_MONITOR_AWARE_V2（Win10 1703+，最准）
    if u is not None:
        try:
            u.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            u.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
            if u.SetProcessDpiAwarenessContext(
                    ctypes.c_void_p(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)):
                result = "per-monitor-v2"
            else:
                _debug(f"  SetProcessDpiAwarenessContext 失败 err={ctypes.get_last_error()}")
        except Exception as e:  # noqa: BLE001
            _debug(f"  SetProcessDpiAwarenessContext 异常 {type(e).__name__}: {e}")
    # 2) Shcore（Win8.1+）
    if result == "none":
        try:
            sh = ctypes.WinDLL("shcore", use_last_error=True)
            if sh.SetProcessDpiAwareness(2) == 0:
                result = "per-monitor"
            elif sh.SetProcessDpiAwareness(1) == 0:
                result = "system"
        except Exception:  # noqa: BLE001
            pass
    # 3) 老 API（Vista+）
    if result == "none" and u is not None:
        try:
            if u.SetProcessDPIAware():
                result = "system-legacy"
        except Exception:  # noqa: BLE001
            pass
    _AWARENESS[0] = result
    _debug(f"enable_dpi_awareness: 结果={result} GetDpiForSystem={window_dpi(None)}")
    return result


__all__ = ["SCALE_ENV", "MIN_SCALE", "MAX_SCALE", "env_scale", "dpi_scale",
           "window_dpi", "dpi_report", "enable_dpi_awareness"]
