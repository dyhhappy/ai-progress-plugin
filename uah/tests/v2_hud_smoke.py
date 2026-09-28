"""UAH v2 HUD 真实运行冒烟 —— 真 Hub、真 SSE、真窗口、真截图。

**不是 mock**：起一个真实的 ``HubServer``，HUD 用真实 HTTP+SSE 连接，
事件通过真实的 ``POST /event`` 灌进去。每一步都截图到 artifacts 目录，
供人工（以及多模态检查）核对：

* 深蓝黑主题、中文渲染、状态灯颜色
* Mini / Compact / Expanded 三形态与三页 tab
* 贴附式 Alert 提示条（L4 橙 / L5 红 + 真实热键）
* OC 印章**真实素材**已接入品牌区（不是占位章）

用法（必须用带 tkinter + Pillow 的解释器）：

    <python312> uah/tests/v2_hud_smoke.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

_THIS = Path(__file__).resolve()
_ROOT = _THIS.parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from uah.core.protocol import PROTOCOL_VERSION  # noqa: E402
from uah.core.transport import HubServer, pick_free_port  # noqa: E402
from uah.hosts.desktop.compact import TABS  # noqa: E402
from uah.theme import tokens as T  # noqa: E402

OUT = _ROOT / "artifacts" / "uah_v2_hud"
RESULTS: list[tuple[str, bool, str]] = []
_SHOTS: dict[str, str] = {}


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"   [{detail}]" if detail else ""),
          flush=True)


def _grab(window, filename: str) -> str:
    if window is None:
        return "no window"
    try:
        from PIL import ImageGrab
    except Exception as exc:  # noqa: BLE001
        return f"no Pillow: {exc}"
    window.update_idletasks()
    x, y = window.winfo_rootx(), window.winfo_rooty()
    w, h = window.winfo_width(), window.winfo_height()
    if w <= 1 or h <= 1:
        return "window not mapped"
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / filename
    ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(path)
    return str(path)


def shot(app, filename: str) -> str:
    path = _grab(app._root, filename)
    _SHOTS[filename] = path
    return path


def crop_brand(app, filename: str, *, box=(0, 6, 225, 54)) -> str:
    """从**真实截图**里裁出品牌区并放大 2×（证明 OC 已真接入品牌区）。"""
    try:
        from PIL import Image, ImageGrab
    except Exception as exc:  # noqa: BLE001
        return f"no Pillow: {exc}"
    root = app._root
    root.update_idletasks()
    x, y = root.winfo_rootx(), root.winfo_rooty()
    x0, y0, x1, y1 = box
    img = ImageGrab.grab(bbox=(x + x0, y + y0, x + x1, y + y1))
    img = img.resize((int(img.width * 1.6), int(img.height * 1.6)), Image.LANCZOS)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / filename
    img.save(path)
    _SHOTS[filename] = str(path)
    return str(path)


def pump(app, seconds: float, step: float = 0.05) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app._root.update()
        time.sleep(step)


def post(url: str, payload: dict) -> dict:
    import json
    import urllib.request

    req = urllib.request.Request(
        url.rstrip("/") + "/event",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=3) as resp:
        return json.loads(resp.read().decode("utf-8"))


_SEQ = [0]


def event(**kw) -> dict:
    """构造一条真实事件。seq **严格递增**，否则会被 StateStore 的乱序保护挡下。"""
    _SEQ[0] += 1
    base = {
        "protocol": PROTOCOL_VERSION,
        "event_id": f"smoke-{_SEQ[0]}-{time.time_ns()}",
        "seq": _SEQ[0],
        "type": "status.changed",
        "timestamp": time.time(),
        "agent": {"id": "uha", "name": "UHA", "type": "uha"},
    }
    base.update(kw)
    return base


def main() -> int:
    print("=== UAH v2 HUD 真实运行冒烟（UI 收口轮）===", flush=True)
    from uah.hosts.desktop.compact import HudApp, enable_dpi_awareness

    print("DPI:", enable_dpi_awareness(), flush=True)

    port = pick_free_port()
    server = HubServer(port=port)
    server.start()
    url = server.url()
    print(f"Hub: {url}  protocol={PROTOCOL_VERSION}", flush=True)

    app = HudApp(url=url, mode="compact", notify_sound=False, notify_toast=False)
    app.build()
    app.start_stream()
    check("HUD 启动成功", app._root is not None and app._root.winfo_exists() == 1)

    # --- 0) 品牌区：状态灯 + UAH + OC（真实素材） --------------------------
    check("三种形态都有品牌区", set(app._brands) == set(app.MODES), str(sorted(app._brands)))
    check("品牌区含状态灯与 UAH 字样",
          all(b.dot.winfo_exists() == 1 and b.name.cget("text") == "UAH"
              for b in app._brands.values()))
    check("OC 印章为**真实素材**（非占位章）",
          all(b.stamp_available for b in app._brands.values()),
          "stamps=" + ",".join(str(b.stamp_available) for b in app._brands.values()))

    # --- 1) IDLE -----------------------------------------------------------
    post(url, event(status="IDLE", type="status.changed",
                    activity={"summary": "随时待命"}, task={"name": "空闲"}))
    pump(app, 1.2)
    check("IDLE 状态文案为中文",
          app._state_labels["compact"].cget("text").startswith("待机中"),
          app._state_labels["compact"].cget("text"))
    shot(app, "01_compact_idle.png")
    crop_brand(app, "06_oc_brand_crop.png")

    # --- 2) RUNNING（真实工具 → 中文执行方式） ----------------------------
    post(url, event(status="RUNNING", type="task.stage_changed",
                    task={"name": "调整屋顶模型", "phase": "Phase 4B", "stage": "结构化执行",
                          "step": 3, "total_steps": 7},
                    activity={"summary": "正在调整屋顶模型", "tool": "UNREAL_MCP"}))
    pump(app, 1.2)
    text = app._state_labels["compact"].cget("text")
    check("RUNNING 显示中文状态 + 执行方式（MCP · Unreal）",
          "运行中" in text and "MCP" in text, text)
    check("活动行来自真实 activity",
          "调整屋顶模型" in app._activity_labels["compact"].cget("text"))
    check("状态灯为运行蓝", app._dots["compact"].cget("fg").upper() == T.INFO.upper(),
          app._dots["compact"].cget("fg"))
    check("elapsed 在走", bool(app._elapsed_labels["compact"].cget("text")))
    check("Compact 关键操作为图标（暂停/展开/停止）",
          len(app._pause_buttons) >= 2 and len(app._stop_buttons) >= 2,
          f"pause={len(app._pause_buttons)} stop={len(app._stop_buttons)}")
    shot(app, "02_compact_running.png")

    # --- 3) 图标按钮中文悬停提示 ------------------------------------------
    icon_btn = app._pause_buttons[0]
    icon_btn.event_generate("<Enter>")
    pump(app, 0.3)
    hint = app._hint_label.cget("text") if app._hint_label else ""
    check("图标按钮有中文悬停提示", "暂停" in hint, hint or "(空)")
    icon_btn.event_generate("<Leave>")

    # --- 4) Expanded：三页 tab --------------------------------------------
    app._apply_form("expanded")
    pump(app, 2.6)
    check("展开面板有 3 个页签",
          [k for k, _ in TABS] == list(app._tab_frames), str(sorted(app._tab_frames)))
    app._set_tab("task")
    pump(app, 0.6)
    check("「当前任务」页显示任务名",
          "调整屋顶模型" in app._detail_widgets["task"].cget("text"),
          app._detail_widgets["task"].cget("text"))
    tl = app._timeline_widgets["label"].cget("text")
    check("「当前任务」页最近活动来自真实事件流",
          "调整屋顶模型" in tl or "随时待命" in tl, tl.replace("\n", " / ")[:70])
    shot(app, "03_expanded_running.png")

    app._set_tab("method")
    pump(app, 0.8)
    check("「执行方式」页显示中文执行方式",
          "MCP" in app._method_widgets["method"].cget("text"),
          app._method_widgets["method"].cget("text"))
    check("「执行方式」页安全行存在",
          "HUD 不依赖" in app._safety_label.cget("text")
          or "急停" in app._safety_label.cget("text"),
          app._safety_label.cget("text"))
    shot(app, "07_expanded_method.png")

    app._set_tab("settings")
    pump(app, 0.6)
    check("「设置」页有静音且显示急停热键",
          app._settings_widgets["mute"].winfo_exists() == 1
          and "ALT" in app._settings_widgets["hotkey"].cget("text"),
          app._settings_widgets["hotkey"].cget("text"))
    shot(app, "08_expanded_settings.png")
    app._set_tab("task")

    # --- 5) WAITING_APPROVAL → L3（不弹告警条） ---------------------------
    post(url, event(status="WAITING_APPROVAL", type="approval.required",
                    attention=3, attention_reason="等待批准：移动 Actor",
                    activity={"summary": "等待批准：移动 Actor"},
                    task={"name": "调整屋顶模型", "stage": "Waiting"}))
    pump(app, 1.2)
    check("等待态文字为「等待批准」", "等待批准" in app._state_labels["expanded"].cget("text"),
          app._state_labels["expanded"].cget("text"))
    app._set_tab("method")
    pump(app, 0.6)
    att_text = app._method_widgets["attention"].cget("text")
    check("Attention 显示 L3", "L3" in att_text, att_text)
    check("L3 不触发提示条（层级克制）", app._alert_level < 4, str(app._alert_level))
    app._set_tab("task")

    # --- 6) L4 → 贴附式提示条（橙） ---------------------------------------
    post(url, event(status="ERROR", type="task.failed", attention=4,
                    attention_reason="复验失败：Transform 未生效",
                    activity={"summary": "复验失败：Transform 未生效"}))
    pump(app, 1.5)
    check("L4 贴出提示条（窗口内，不是独立悬浮窗）",
          app._alert_level == 4 and app._alert.winfo_ismapped() == 1,
          f"level={app._alert_level} mapped={app._alert.winfo_ismapped()}")
    check("L4 提示条文案含高优先级提示",
          "高优先级提示" in app._alert_title.cget("text"), app._alert_title.cget("text"))
    shot(app, "05_alert_l4.png")

    # --- 7) L5 → 安全条目 + 红色提示条 + 真实热键 --------------------------
    post(url, event(agent={"id": "uha-safety", "name": "UHA 安全层", "type": "safety"},
                    status="RUNNING", type="input.control_acquired", attention=5,
                    attention_reason="输入控制已接管 · 鼠标键鼠由 Agent 驱动",
                    activity={"summary": "输入控制已接管 · 鼠标键鼠由 Agent 驱动",
                              "detail": "hotkey=ctrl+alt+f12", "tool": "SafetyGate"},
                    task={"name": "键鼠安全", "stage": "ACTIVE"}))
    pump(app, 1.5)
    check("L5 提示条升级为红色（安全优先）", app._alert_level == 5, str(app._alert_level))
    check("L5 提示条标题点明「输入控制未释放」",
          "输入控制未释放" in app._alert_title.cget("text"), app._alert_title.cget("text"))
    app._root.update_idletasks()
    alert_all = app._alert_texts()
    check("提示条显示**真实**急停热键（来自安全状态）",
          "CTRL + ALT + F12" in alert_all, alert_all)
    check("热键单独放在高对比胶囊里（带边框强调）",
          app._alert_hotkey.winfo_exists() == 1
          and bool(str(app._alert_hotkey.cget("highlightbackground"))),
          str(app._alert_hotkey.cget("text")))
    check("提示条有明确的用户动作文案",
          "请使用快捷键紧急停止" in alert_all, alert_all)
    check("提示条左侧竖条加粗（≥3 物理像素）",
          app._alert_bar.winfo_width() >= 3, str(app._alert_bar.winfo_width()))
    app._set_tab("method")
    pump(app, 0.6)
    safety_text = app._safety_label.cget("text")
    check("安全状态进入 UI", "输入控制已接管" in safety_text, safety_text)
    app._set_tab("task")
    pump(app, 0.4)
    shot(app, "04_alert_l5.png")
    shot(app, "04b_alert_l5_expanded.png")

    # --- 8) 关闭提示条 → 窗口高度回落 -------------------------------------
    height_with_alert = app._root.winfo_height()
    app._dismiss_alert()
    pump(app, 0.6)
    check("可关闭提示条且窗口高度回落",
          app._alert.winfo_ismapped() == 0
          and app._root.winfo_height() < height_with_alert,
          f"{height_with_alert} -> {app._root.winfo_height()}")

    # --- 9) Mini ----------------------------------------------------------
    app._apply_form("mini")
    pump(app, 1.0)
    exp_w = int(round(T.FORM_SIZES["mini"][0] * app._scale))
    check("Mini 尺寸正确（只有 状态灯+UAH+OC+展开）",
          app._root.winfo_width() == exp_w, f"{app._root.winfo_width()} 期望 {exp_w}")
    check("Mini 不显示日志/工具数量", "mini" not in app._activity_labels)
    check("Mini 品牌区同样有 OC", app._brands["mini"].stamp_available)
    shot(app, "09_mini.png")
    crop_brand(app, "09b_mini_brand_crop.png", box=(0, 4, 235, 46))

    # --- 10) 软控制通道（真实 HTTP） --------------------------------------
    app._apply_form("compact")
    pump(app, 0.4)
    app._control("pause")
    pump(app, 0.6)
    cmds = server.take_control_commands(wait_s=0.1)
    check("Hub 收到 pause 命令（图标按钮真的接通）",
          any(c.get("action") == "pause" for c in cmds), str([c.get("action") for c in cmds]))

    # --- 11) 断连 / 重连恢复 ---------------------------------------------
    server.stop()
    pump(app, 2.0)
    check("Hub 停止后 HUD 仍存活", app._root.winfo_exists() == 1,
          f"stream_status={app._stream_status}")
    server2 = HubServer(port=port)
    server2.start()
    post(url, event(status="RUNNING", type="task.resumed",
                    activity={"summary": "已恢复执行", "tool": "UE_PYTHON"},
                    task={"name": "调整屋顶模型"}))
    pump(app, 3.0)
    check("重连后恢复真实状态（不是错误显示待机）",
          "运行中" in app._state_labels["compact"].cget("text"),
          app._state_labels["compact"].cget("text"))
    check("Script 通道显示为中文 Script 标签",
          "Script" in app._state_labels["compact"].cget("text"),
          app._state_labels["compact"].cget("text"))

    # --- 12) HUD 重启：恢复真实状态 ---------------------------------------
    app2 = HudApp(url=url, mode="compact", notify_sound=False, notify_toast=False)
    app2.build()
    app2.start_stream()
    end = time.time() + 2.5
    while time.time() < end:
        app2._root.update()
        time.sleep(0.05)
    text2 = app2._state_labels["compact"].cget("text")
    check("HUD 重启后恢复当前 UHA 状态（不是待机）", "运行中" in text2, text2)
    app2.close()

    server2.stop()
    app.close()

    # --- 13) DPI 矩阵：100% / 125% / 150% 真实建窗口并检查不裁切 ------------
    # 用 UAH_HUD_SCALE 强制缩放系数（真实 DPI 感知仍然生效，这只是把系数固定下来，
    # 以便在同一台机器上比较三种缩放下的真实渲染结果）。
    for scale in (1.0, 1.25, 1.5):
        os.environ["UAH_HUD_SCALE"] = str(scale)
        port = pick_free_port()
        srv = HubServer(port=port).start()
        h = HudApp(url=srv.url(), mode="compact", notify_sound=False, notify_toast=False)
        h.build()
        h.start_stream()
        pump(h, 1.2)
        post(srv.url(), event(status="RUNNING", type="task.stage_changed",
                              task={"name": "调整屋顶模型", "phase": "Phase 4B",
                                    "stage": "结构化执行", "step": 3, "total_steps": 7},
                              activity={"summary": "正在调整屋顶模型",
                                        "tool": "UNREAL_MCP"}))
        pump(h, 1.2)
        exp_w = int(round(T.FORM_SIZES["compact"][0] * scale))
        exp_h = int(round(T.FORM_SIZES["compact"][1] * scale))
        # 宽度 = 逻辑宽 × 缩放（精确）；高度 = max(令牌下限, 内容实测) —— 只许更大不许裁切
        check(f"[{scale:g}x] Compact 物理宽度符合逻辑尺寸×缩放",
              h._root.winfo_width() == exp_w,
              f"{h._root.winfo_width()} 期望 {exp_w}")
        check(f"[{scale:g}x] Compact 高度不低于逻辑下限（内容驱动但只增不减）",
              h._root.winfo_height() >= exp_h,
              f"{h._root.winfo_height()} 下限 {exp_h}")
        # 不裁切：整个 compact 容器的"需求宽度"必须小于窗口宽度（行内元素不能相加，
        # 品牌行与活动行是两行；所以看容器自身的 reqwidth 才准）
        h._root.update_idletasks()
        need_w = int(h._forms["compact"].winfo_reqwidth())
        need_h = int(h._forms["compact"].winfo_reqheight())
        check(f"[{scale:g}x] Compact 内容不裁切（容器需求 vs 窗口）",
              need_w <= h._root.winfo_width() and need_h <= h._root.winfo_height(),
              f"need={need_w}x{need_h} win={h._root.winfo_width()}x{h._root.winfo_height()}")
        check(f"[{scale:g}x] OC 印章在该缩放下取到合适档位",
              h._brands["compact"].stamp is not None
              and h._brands["compact"].stamp.available,
              f"stamp_px={h._brands['compact'].stamp.size if h._brands['compact'].stamp else 0}")
        shot(h, f"10_compact_{int(scale * 100)}pct.png")
        crop_brand(h, f"11_oc_stamp_zoom_{int(scale * 100)}pct.png",
                   box=(0, T.px(6, scale), T.px(235, scale), T.px(52, scale)))
        h.close()
        srv.stop()
    os.environ.pop("UAH_HUD_SCALE", None)

    print("\n=== 截图 ===")
    for name, path in _SHOTS.items():
        print(f"  {name}: {path}")
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{'全部通过' if not failed else '失败'}（{len(RESULTS) - len(failed)}/{len(RESULTS)}）")
    for name, _ok, detail in failed:
        print(f"  - {name}  {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    code = main()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001
            pass
    os._exit(code)
