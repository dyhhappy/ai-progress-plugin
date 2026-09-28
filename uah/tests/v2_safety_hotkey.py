"""全局急停热键的**真实**验证（真的注册系统热键 + 真的注入按键 + 真的触发回调）。

验收项 #11 / #12 / #14 的证据来源：

* 热键用 ``RegisterHotKey`` 注册在系统级 —— 不依赖 UAH 窗口焦点、不依赖鼠标；
* 用 ``SendInput`` 注入 ctrl+alt+f12（合成按键，走 Windows 输入栈）；
* 回调被触发即证明"键盘就能急停"；
* 再验证"记账式释放"只放 UAH 自己按下的键，不干扰用户正按住的键。

注意：本测试会在系统上**临时占用** ctrl+alt+f12（几秒），结束后立即注销。

用法：``python uah/tests/v2_safety_hotkey.py``
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.desktop import hotkey as hk  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"   [{detail}]" if detail else ""), flush=True)


def send_combo(*, hold_s: float = 0.35, repeat: int = 1) -> None:
    """注入 ctrl+alt+f12（真实输入栈，复用 UHA 自己的 INPUT 结构体）。

    **必须**用 ``src.desktop.hotkey`` 里的结构体：INPUT 的 union 必须包含
    MOUSEINPUT（比 KEYBDINPUT 大），否则 ``SendInput`` 直接返回 0 /
    ERROR_INVALID_PARAMETER(87)——自建简化结构体踩过这个坑。

    ``hold_s`` 是按住时长：RegisterHotKey 被系统拒绝时会退回 50ms 采样轮询，
    瞬时按键会被采样漏掉；按住 0.35s 才能真实覆盖轮询回退路径。
    """
    VK_CONTROL, VK_MENU, VK_F12 = 0x11, 0x12, 0x7B

    def ev(vk: int, up: bool) -> "hk.INPUT":
        item = hk.INPUT(type=hk.INPUT_KEYBOARD)
        item.union.ki = hk.KEYBDINPUT(wVk=vk, wScan=0,
                                      dwFlags=hk.KEYEVENTF_KEYUP if up else 0,
                                      time=0, dwExtraInfo=None)
        return item

    for _ in range(max(1, repeat)):
        hk._send_input(ev(VK_CONTROL, False), ev(VK_MENU, False), ev(VK_F12, False))
        time.sleep(hold_s)
        hk._send_input(ev(VK_F12, True), ev(VK_MENU, True), ev(VK_CONTROL, True))
        time.sleep(0.4)


def main() -> int:
    print("=== 全局急停热键真实验证 ===", flush=True)
    if sys.platform != "win32":
        print("  非 Windows 平台：跳过（NOT TESTED）")
        return 0

    mods, vk = hk.parse_hotkey("ctrl+alt+f12")
    check("热键解析正确（ctrl+alt+f12）", vk == 0x7B and bool(mods), f"mods={mods:#x} vk={vk:#x}")
    # 立项决定：急停热键 = Ctrl+Alt+F12（**不含 Shift**）。这里把三个默认值都锁住，
    # 防止以后有人"顺手"改回 ctrl+alt+shift+f12 造成文档与实现不一致。
    default_mods, default_vk = hk.parse_hotkey("")
    want = hk.MOD_CONTROL | hk.MOD_ALT | hk.MOD_NOREPEAT
    check("未写修饰键时默认 Ctrl+Alt（无 Shift）+ F12",
          default_mods == want and default_vk == 0x7B,
          f"mods={default_mods:#x} vk={default_vk:#x} 期望 {want:#x}/0x7b")
    import inspect
    sig = inspect.signature(hk.EmergencyHotkey.__init__)
    check("EmergencyHotkey 默认热键 = ctrl+alt+f12",
          sig.parameters["hotkey"].default == "ctrl+alt+f12",
          str(sig.parameters["hotkey"].default))

    fired: list[float] = []
    hot = hk.EmergencyHotkey(lambda: fired.append(time.time()), hotkey="ctrl+alt+f12")
    hot.start()
    time.sleep(0.6)
    mode = hot.mode
    check("急停热键已就绪（registered=系统注册 / polling=本机回退）",
          mode in ("registered", "polling"), f"mode={mode}")

    for attempt in range(1, 4):
        send_combo(hold_s=0.35, repeat=2 if mode == "polling" else 1)
        if fired:
            break
        time.sleep(0.8)
    check("键盘即可急停：注入 ctrl+alt+f12 后回调被触发（不依赖鼠标/窗口焦点）",
          len(fired) > 0, f"mode={mode} fired={len(fired)}")
    hot.stop()
    check("热键已注销（不长期占用系统热键）", hot.registered is False, str(hot.registered))

    # 记账式释放：只放 UHA 自己按下的键
    hk.clear_tracking()
    hk.track_key_press("ctrl")
    out = hk.release_all_keys_and_buttons()
    check("释放范围为 UHA 记账键（scope=uha_tracked_only）",
          out.get("scope") == "uha_tracked_only", str(out.get("scope")))
    check("未跟踪的键不会被无差别抬起（unresolved 为空）",
          out.get("unresolved") in ([], None), str(out.get("unresolved")))
    hk.clear_tracking()

    try:
        hk.release_everything_now()
        check("无差别释放需要 explicit=True（否则拒绝）", False, "没有抛异常")
    except RuntimeError:
        check("无差别释放需要 explicit=True（否则拒绝）", True)

    failed = [c for c in CHECKS if not c[1]]
    print(f"\n{'全部通过' if not failed else '失败'}（{len(CHECKS) - len(failed)}/{len(CHECKS)}）")
    for name, _ok, detail in failed:
        print(f"  - {name}  {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
