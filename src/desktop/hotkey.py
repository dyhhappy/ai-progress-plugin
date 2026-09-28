"""Emergency Stop：确定性释放键鼠 + 热键注册。

* **不**依赖 LLM / Vision / Router。
* 释放：Ctrl/Alt/Shift/Win 修饰键、常见按键 keyup、鼠标左右中键 buttonup。
* 热键默认 `Ctrl+Alt+Shift+F12`（与 UE 常用快捷键冲突概率低），可通过
  config `desktop.emergency_hotkey` 修改，例如 `ctrl+alt+f10`。

热键实现优先 **ctypes RegisterHotKey**（零第三方依赖）。
若不可用，退回轮询 GetAsyncKeyState（仍为本地确定性逻辑）。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import threading
import time
from typing import Callable

user32 = ctypes.WinDLL("user32", use_last_error=True)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
VK_MAP = {
    "ctrl": 0x11, "control": 0x11,
    "alt": 0x12, "menu": 0x12,
    "shift": 0x10,
    "win": 0x5B, "meta": 0x5B,
}

# 需要在紧急停止时强制 keyup 的虚拟键
_RELEASE_VKS = [
    0x10, 0x11, 0x12, 0x5B, 0x5C,  # shift ctrl alt lwin rwin
    0x41, 0x42, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49, 0x4A,
    0x4B, 0x4C, 0x4D, 0x4E, 0x4F, 0x50, 0x51, 0x52, 0x53, 0x54,
    0x55, 0x56, 0x57, 0x58, 0x59, 0x5A,
    0x0D, 0x1B, 0x20, 0x09,  # enter esc space tab
]

INPUT_KEYBOARD = 1
INPUT_MOUSE = 0
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTUP = 0x0008
MOUSEEVENTF_MIDDLEUP = 0x0020


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.wintypes.LONG),
        ("dy", ctypes.wintypes.LONG),
        ("mouseData", ctypes.wintypes.DWORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.wintypes.WORD),
        ("wScan", ctypes.wintypes.WORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUTunion(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.wintypes.DWORD), ("union", _INPUTunion)]


def _send_input(*inputs: INPUT) -> None:
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    user32.SendInput(n, ctypes.byref(arr), ctypes.sizeof(INPUT))


def release_all_keys_and_buttons() -> dict[str, list[str]]:
    """紧急停止核心：抬起修饰键/字母键 + 松开鼠标键。返回释放清单。"""
    keys: list[str] = []
    buttons: list[str] = []
    for vk in _RELEASE_VKS:
        try:
            inp = INPUT(type=INPUT_KEYBOARD)
            inp.union.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=KEYEVENTF_KEYUP, time=0, dwExtraInfo=None)
            _send_input(inp)
            keys.append(hex(vk))
        except Exception:
            continue
    for flag, name in (
        (MOUSEEVENTF_LEFTUP, "left"),
        (MOUSEEVENTF_RIGHTUP, "right"),
        (MOUSEEVENTF_MIDDLEUP, "middle"),
    ):
        try:
            inp = INPUT(type=INPUT_MOUSE)
            inp.union.mi = MOUSEINPUT(0, 0, 0, flag, 0, None)
            _send_input(inp)
            buttons.append(name)
        except Exception:
            continue
    return {"keys_released": keys, "buttons_released": buttons}


def parse_hotkey(spec: str) -> tuple[int, int]:
    """`ctrl+alt+f12` -> (modifiers, vk)。"""
    parts = [p.strip().lower() for p in str(spec or "").split("+") if p.strip()]
    mods = 0
    vk = 0
    for p in parts:
        if p in VK_MAP:
            mods |= VK_MAP[p]
            continue
        if p.startswith("f") and p[1:].isdigit():
            n = int(p[1:])
            if 1 <= n <= 24:
                vk = 0x70 + (n - 1)  # VK_F1=0x70
                continue
        if len(p) == 1:
            vk = ord(p.upper())
            continue
        if p.startswith("0x"):
            vk = int(p, 16)
    if vk == 0:
        vk = 0x7B  # F12
    if mods == 0:
        mods = MOD_CONTROL | MOD_ALT
    return mods | MOD_NOREPEAT, vk


class EmergencyHotkey:
    """注册系统热键；触发时执行确定性 emergency 回调。"""

    def __init__(self, callback: Callable[[], None], *, hotkey: str = "ctrl+alt+f12"):
        self.callback = callback
        self.hotkey_spec = hotkey
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._hotkey_id = 1
        self.registered = False
        self._use_polling = False
        self._mods, self._vk = parse_hotkey(hotkey)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="uha-estop-hotkey", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.registered:
            try:
                user32.UnregisterHotKey(None, self._hotkey_id)
            except Exception:
                pass
            self.registered = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        # 尝试 RegisterHotKey
        ok = False
        try:
            ok = bool(user32.RegisterHotKey(None, self._hotkey_id, self._mods, self._vk))
        except Exception:
            ok = False
        if not ok:
            self._use_polling = True
            self._poll_loop()
            return
        self.registered = True
        msg = ctypes.wintypes.MSG()
        while not self._stop.is_set():
            has = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if has == 0:
                break
            if has == -1:
                break
            if msg.message == WM_HOTKEY and msg.wParam == self._hotkey_id:
                try:
                    self.callback()
                except Exception:
                    pass
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        if self.registered:
            try:
                user32.UnregisterHotKey(None, self._hotkey_id)
            except Exception:
                pass
            self.registered = False

    def _poll_loop(self) -> None:
        """回退：轮询修饰键+主键是否同时按下。"""
        mods_map = [
            (self._mods & MOD_CONTROL, 0x11),
            (self._mods & MOD_ALT, 0x12),
            (self._mods & MOD_SHIFT, 0x10),
            (self._mods & MOD_WIN, 0x5B),
        ]
        while not self._stop.is_set():
            try:
                all_down = all(user32.GetAsyncKeyState(vk) & 0x8000 for flag, vk in mods_map if flag)
                main_down = bool(user32.GetAsyncKeyState(self._vk) & 0x8000)
                if all_down and main_down:
                    self.callback()
                    time.sleep(0.5)  # 防抖
            except Exception:
                pass
            time.sleep(0.05)

    @property
    def mode(self) -> str:
        return "polling" if self._use_polling else ("registered" if self.registered else "stopped")


def build_emergency_handler(controller, coordinator=None, computer_use=None) -> Callable[[], None]:
    """组装紧急停止钩子：释放键鼠 → 释放 DesktopLock → 标记 ABORTED。"""

    def _handler() -> None:
        release_all_keys_and_buttons()
        if computer_use is not None:
            try:
                # 硬释放桌面锁（force）
                if hasattr(computer_use, "release_control"):
                    computer_use.release_control(force=True)
            except Exception:
                pass
        if coordinator is not None:
            try:
                coordinator.force_release_all()
            except Exception:
                pass
        try:
            controller.emergency_stop(reason="EMERGENCY_STOP")
        except Exception:
            pass

    return _handler


__all__ = [
    "release_all_keys_and_buttons",
    "parse_hotkey",
    "EmergencyHotkey",
    "build_emergency_handler",
]
