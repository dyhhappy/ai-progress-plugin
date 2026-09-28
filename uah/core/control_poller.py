"""ControlPoller —— 把 HUD 提交的控制命令送到 SessionController。

方向与安全边界：

    HUD 按钮 ──POST /control──▶ Hub 队列 ──长轮询──▶ 本轮询器 ──▶ SessionController

* 只支持 ``pause / resume / stop`` 三个**软控制**命令。
* **不支持** emergency_stop：急停只走全局热键 + SafetyController（需求 §十四）。
  HUD 里永远不会有"急停"按钮，只有急停热键的提示文案。
* 长轮询（默认 8s）挂在 daemon 线程上；命令执行失败只写日志，不重试不阻塞。
* SessionController.pause/resume/stop 自带护栏（不得清掉已有 PAUSE/STOP/ESTOP），
  本轮询器只调用公开方法，不做任何第二套控制逻辑。
"""

from __future__ import annotations

import threading
import time
import traceback
from typing import Any, Callable


class ControlPoller:
    """长轮询 Hub 控制队列并应用到 SessionController。"""

    def __init__(
        self,
        url: str,
        controller: Any,
        *,
        wait_s: float = 0.5,
        agent_id: str = "uha",
        log_fn: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.controller = controller
        self.agent_id = agent_id
        self._last_message = ""
        self.wait_s = float(wait_s)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._log_fn = log_fn
        self.stats = {"fetched": 0, "applied": 0, "failed": 0, "last_error": ""}

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="uah-control-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1.5)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            cmds: list[dict[str, Any]] = []
            try:
                from .approval import fetch_control_commands

                cmds = fetch_control_commands(self.url, wait_s=self.wait_s, agent_id=self.agent_id)
            except Exception:  # noqa: BLE001 - Hub 不在/网络抖动：安静地下一轮
                cmds = []
            if not cmds:
                self._stop.wait(0.1)
                continue
            for cmd in cmds:
                self.stats["fetched"] += 1
                applied = self._apply(cmd)
                self.stats["applied" if applied else "failed"] += 1
                if cmd.get("id"):
                    try:
                        from .approval import acknowledge_control
                        acknowledge_control(self.url, cmd, applied, self._last_message)
                    except Exception as exc:
                        self.stats["last_error"] = str(exc)[:200]

    def _apply(self, cmd: dict[str, Any]) -> bool:
        action = str(cmd.get("action", "")).lower()
        self._last_message = "控制请求失败"
        if cmd.get("agent", self.agent_id) != self.agent_id or time.time() >= cmd.get("expires_at", float("inf")):
            self._last_message = "请求目标不匹配或已过期"
            return False
        try:
            state = self.controller.snapshot()
            command = str(getattr(state.command, "value", state.command)).upper()
            phase = str(getattr(state.phase, "value", state.phase)).upper()
            if command in ("EMERGENCY_STOP", "STOP") or phase == "ABORTED":
                self._last_message = "任务已中止，不能覆盖停止状态"
                return False
            if action == "resume" and command != "PAUSE":
                self._last_message = "任务没有处于暂停状态"
                return False
            if action == "pause" and phase in ("IDLE", "DONE", "FAILED"):
                self._last_message = "没有正在执行的任务"
                return False
            if action == "pause":
                self.controller.pause()
            elif action == "resume":
                self.controller.resume()
            elif action == "stop":
                self.controller.stop()
            else:
                # 未知命令（包括一切形式的 estop）：拒绝执行并记录。
                self.stats["last_error"] = f"rejected action {action!r}"
                self._log("control_rejected", action=action, reason="not a soft-control action")
                return False
        except Exception as exc:  # noqa: BLE001
            self.stats["last_error"] = f"{type(exc).__name__}: {exc}"
            self._log("control_apply_error", action=action, error=f"{type(exc).__name__}: {exc}",
                      trace=traceback.format_exc(limit=3))
            return False
        state = self.controller.snapshot()
        command = getattr(state.command, "value", state.command)
        expected = {"pause": "PAUSE", "resume": "NONE", "stop": "STOP"}[action]
        if str(command).upper() != expected:
            self._last_message = "当前状态不允许该操作"
            return False
        self._last_message = {"pause": "暂停指令已生效；已提交操作可能仍在收尾",
                              "resume": "继续指令已生效", "stop": "停止指令已生效；等待执行收尾"}[action]
        self._log("control_applied", action=action, source=str(cmd.get("source", "hud")))
        return True

    def _log(self, kind: str, **fields: Any) -> None:
        if self._log_fn is None:
            return
        try:
            self._log_fn(kind, {"ts": time.time(), **fields})
        except Exception:  # noqa: BLE001
            pass


__all__ = ["ControlPoller"]
