"""UAH v2 桌面 HUD 的真实窗口测试（不是合成事件，是**真的建窗口**）。

覆盖（全部在真实 Tk 窗口上跑）：

* 窗口无边框 / 置顶 / **不抢前台焦点** / WS_EX_NOACTIVATE
* Mini / Compact / Expanded 三形态切换与真实像素尺寸
* 拖拽移动 + 位置持久化 + 重启后恢复形态与位置
* 通知**不抬窗、不抢焦点**（v1 的 lift/deiconify 会压到安全横幅上，v2 已去掉）
* 审批：pending 请求 → 出现在 Expanded → 点「批准/拒绝」真实写回审批通道
* Hub 重启 → SSE 重连、旧 Agent 不残留、定时器不泄漏
* 关闭 → SSE 线程退出、窗口销毁

用法：``<python312> uah/tests/compact_smoke.py``
"""
import ctypes
import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from uah.adapters.generic.bridge import GenericBridge  # noqa: E402
from uah.core.approval import approval_call, token_path  # noqa: E402
from uah.core.transport import HubServer  # noqa: E402
from uah.hosts.desktop.compact import HudApp  # noqa: E402
from uah.theme import tokens as T  # noqa: E402
from uah.ui.notify import Notifier  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"   [{detail}]" if detail else ""), flush=True)


def main() -> int:
    print("=== UAH v2 HUD 真实窗口测试 ===", flush=True)
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "window.json"
        server = HubServer(port=0).start()
        app = HudApp(server.url(), settings_path=path, notifier=Notifier(sinks=[]),
                     poll_ms=30, clock_ms=80)
        foreground = ctypes.windll.user32.GetForegroundWindow()
        root = app.build()
        app.start_stream()

        def pump(seconds: float = 0.3) -> None:
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                root.update()
                time.sleep(0.01)

        try:
            pump(0.4)
            w, h = root.winfo_width(), root.winfo_height()
            exp_w = int(round(T.FORM_SIZES["compact"][0] * app._scale))
            exp_h = int(round(T.FORM_SIZES["compact"][1] * app._scale))
            check("窗口尺寸符合 Compact 规格（DPI 缩放后）", (w, h) == (exp_w, exp_h),
                  f"{w}x{h} 期望 {exp_w}x{exp_h} scale={app._scale:.2f}")
            check("无边框 + 置顶", bool(root.overrideredirect()) and bool(root.attributes("-topmost")))
            # 更稳的不变量：HUD 窗口本身不可能是前台窗口（NOACTIVATE 的真实含义）。
            # 直接比较"程序启动前后的前台句柄"会被测试运行器自身的窗口激活干扰（实测抖动）。
            check("HUD 没有成为前台窗口（NOACTIVATE 生效）",
                  ctypes.windll.user32.GetForegroundWindow() != (app.native_hwnd or 0),
                  f"fg={ctypes.windll.user32.GetForegroundWindow()} hwnd={app.native_hwnd}")
            check("带 WS_EX_NOACTIVATE", bool(ctypes.windll.user32.GetWindowLongW(app.native_hwnd, -20) & 0x08000000))

            bridge = GenericBridge(server.url())
            bridge.emit(agent="UHA", status="running",
                        task={"name": "Scene", "stage": "环境感知", "step": 3, "total_steps": 7},
                        activity="环境感知")
            bridge.emit(agent="Other", status="running", task="另一个任务")
            pump(0.5)
            check("两个 Agent 都进快照", len(app._snapshots) == 2, str(sorted(app._snapshots)))

            # 形态切换：真实像素尺寸
            app._apply_form("mini")
            pump(0.3)
            mw = int(round(T.FORM_SIZES["mini"][0] * app._scale))
            mh = int(round(T.FORM_SIZES["mini"][1] * app._scale))
            # 宽度精确；高度 = max(令牌下限, 内容实测) —— 只许更大，不许裁切
            check("Mini 宽度符合逻辑尺寸×缩放",
                  root.winfo_width() == mw, f"{root.winfo_width()} 期望 {mw}")
            check("Mini 高度不低于逻辑下限",
                  root.winfo_height() >= mh,
                  f"{root.winfo_height()} 下限 {mh}")
            check("Mini 只有状态灯 + UAH + OC（无日志/工具/数量）",
                  "mini" not in app._activity_labels and app._dots["mini"].winfo_exists() == 1)

            app._apply_form("expanded")
            pump(0.4)
            eh = int(round(T.FORM_SIZES["expanded"][1] * app._scale))
            check("Expanded 尺寸正确", root.winfo_height() == eh,
                  f"{root.winfo_height()} 期望 {eh}")
            check("Expanded 显示当前任务", bool(app._detail_widgets["task"].cget("text")))

            # 拖拽 + 持久化
            app._apply_form("compact")
            pump(0.2)
            app._drag_start(SimpleNamespace(x_root=root.winfo_x() + 2, y_root=root.winfo_y() + 2))
            app._drag_move(SimpleNamespace(x_root=302, y_root=202))
            pump(0.3)
            check("拖拽移到目标位置", (root.winfo_x(), root.winfo_y()) == (300, 200),
                  f"({root.winfo_x()},{root.winfo_y()})")

            # 通知不抬窗、不抢焦点
            bridge.emit(agent="UHA", status="waiting_approval", activity="Approve changes")
            pump(0.4)
            check("提醒后 HUD 仍不是前台窗口",
                  ctypes.windll.user32.GetForegroundWindow() != (app.native_hwnd or 0),
                  f"fg={ctypes.windll.user32.GetForegroundWindow()}")
            check("提醒不改窗口尺寸（不抬窗）", root.winfo_height() == exp_h)

            old = app.notifier.muted
            app.toggle_mute()
            check("静音可切换", app.notifier.muted != old)

            # 审批：真实 pending → 按钮 → 真实写回
            capability = token_path(server.url()).read_text(encoding="utf-8").strip()
            approval_call(server.url(), "create",
                          {"req_id": "gui-reject", "agent_id": "UHA",
                           "summary": "GUI 测试用请求", "timeout_s": 8}, token=capability)
            app._apply_form("expanded")
            pump(2.6)
            check("待批请求出现在 Expanded", app._approval_row.winfo_ismapped() == 1,
                  app._approval_label.cget("text"))
            app._reject_btn.event_generate("<Button-1>")
            pump(0.6)
            decision = approval_call(server.url(), "poll", {"req_id": "gui-reject"},
                                     token=capability).get("decision")
            check("点「拒绝」真实写回审批通道", decision is False, str(decision))

            approval_call(server.url(), "create",
                          {"req_id": "gui-approve", "agent_id": "UHA",
                           "summary": "GUI 测试用请求 2", "timeout_s": 8}, token=capability)
            pump(2.6)
            app._approve_btn.event_generate("<Button-1>")
            pump(0.6)
            decision = approval_call(server.url(), "poll", {"req_id": "gui-approve"},
                                     token=capability).get("decision")
            check("点「批准」真实写回审批通道", decision is True, str(decision))

            # Hub 重启 → 重连、旧 Agent 不残留
            port = server.port
            server.stop()
            pump(0.4)
            server = HubServer(port=port).start()
            bridge.emit(agent="Recovered", status="done")
            pump(3.0)
            check("Hub 重启后重连且不残留旧 Agent",
                  set(app._snapshots) == {"Recovered"}, str(sorted(app._snapshots)))
            check("定时器句柄未泄漏", len(app._after_ids) < 12, str(len(app._after_ids)))

            app._apply_form("expanded")
            app.close()
            pump(0.3)
            check("SSE 线程已退出",
                  app._stream_thread is None or not app._stream_thread.is_alive())
            saved = json.loads(path.read_text(encoding="utf-8"))
            check("形态与位置已持久化",
                  saved.get("mode") == "expanded" and saved.get("x") == 300
                  and saved.get("y") == 200, str(saved))

            second = HudApp(server.url(), settings_path=path, notifier=Notifier(sinks=[]))
            root2 = second.build()
            root2.update()
            check("重启后恢复形态与位置",
                  second.mode == "expanded" and root2.winfo_x() == 300 and root2.winfo_y() == 200,
                  f"mode={second.mode} pos=({root2.winfo_x()},{root2.winfo_y()})")
            second.close()
        finally:
            try:
                app.close()
            except Exception:  # noqa: BLE001
                pass
            server.stop()

    failed = [c for c in CHECKS if not c[1]]
    print(f"\n{'全部通过' if not failed else '失败'}（{len(CHECKS) - len(failed)}/{len(CHECKS)}）")
    for name, _ok, detail in failed:
        print(f"  - {name}  {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        code = 1
    from uah.hosts.desktop.__main__ import _cleanup_and_exit

    _cleanup_and_exit(None, code)
