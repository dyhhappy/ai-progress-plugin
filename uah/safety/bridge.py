"""SafetyBridge —— 把 P0 安全层状态如实翻译成 UAH 事件。

职责边界（需求 §十四，这是硬的）：

* 本桥**只观察**：订阅 ``SafetyController.add_observer``（只读快照）。
* 本桥**不实现**任何安全机制。急停是 ``EmergencyHotkey`` + ``SafetyController``
  的事；HUD/Alert 只负责把「现在键鼠归谁、急停热键是什么」**画出来**。
* 本桥崩溃 / UAH Hub 不在，都不能影响安全层——回调里只做出队列入队，
  队列满就丢（与 UhaNativeAdapter 同一套非阻塞策略）。

为什么安全状态是**独立的 Agent 条目**（agent id = ``uha-safety``）：

* 任务状态（SessionController）和安全状态（SafetyController）本来就是两套语义：
  一个说「Agent 在干什么」，一个说「键鼠归谁」。混进同一条目等于把 P0 的
  「CU SAFETY > TASK COMPLETION」优先级在 UI 上抹平。
* 独立条目让 StateStore 的「需要人看的排前面」排序天然把安全卡顶上去。
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Mapping

from ..core.attention import Attention
from ..core.models import Activity, AgentRef, Status, TaskState
from ..core.protocol import (
    EV_AGENT_STARTED,
    EV_AGENT_HEARTBEAT,
    EV_AGENT_STOPPED,
    EV_EMERGENCY_STOP,
    EV_INPUT_CONTROL_ACQUIRED,
    EV_INPUT_CONTROL_RELEASED,
    EV_SAFETY_CHANGED,
    PROTOCOL_VERSION,
)
from ..core.transport import new_event_id
from ..adapters.publisher import Publisher

#: 安全条目的稳定 agent id（HUD 靠它识别安全卡，不靠名字猜）
SAFETY_AGENT_ID = "uha-safety"

DEFAULT_QUEUE_SIZE = 128

#: 允许注入的安全状态（与 src/safety/controller.py 的白名单一致）
_INJECTION_STATES = frozenset({"CONTROL_ACQUIRED", "ACTIVE"})
_TERMINAL_STATES = frozenset({"EMERGENCY_STOP", "HUMAN_OVERRIDE"})


class SafetyBridge:
    """SafetyController → UAH 事件。线程安全，非阻塞。"""

    def __init__(
        self,
        publisher: Publisher | None = None,
        *,
        agent_name: str = "UHA 安全层",
        queue_size: int = DEFAULT_QUEUE_SIZE,
    ) -> None:
        self.publisher: Publisher = publisher or _NullPublisher()
        self.agent = AgentRef(id=SAFETY_AGENT_ID, name=agent_name, type="safety")
        self._lock = threading.Lock()
        self._seq = 0
        self._last_key: tuple = ()
        self._last_state: str | None = None
        self._stopped = False
        self._outbox: "queue.Queue[dict[str, Any] | None]" = queue.Queue(maxsize=max(8, int(queue_size)))
        self._worker: threading.Thread | None = None
        self._worker_stop = threading.Event()

    # -- 生命周期 -----------------------------------------------------------

    def attach(self, controller: Any) -> bool:
        """订阅 SafetyController。失败只返回 False，绝不抛。

        顺序是刻意的：**先发 agent.started（在线），再同步当前快照**。
        反过来的话，一个"已经处于 EMERGENCY_STOP / HUMAN_OVERRIDE 的安全层"
        会被后发的那条 IDLE 覆盖——HUD 会把急停显示成待机（真实踩到过）。
        """
        if controller is None:
            return False
        try:
            controller.add_observer(self._on_safety)
        except Exception:  # noqa: BLE001
            return False
        self._ensure_worker()
        self._emit(EV_AGENT_STARTED, status=Status.IDLE, dedupe_key=None,
                   activity=Activity(summary="安全层在线"),
                   payload={"hotkey": self._hotkey_of(None)})
        # 立刻同步一次当前安全状态，让 HUD 不用等下一次安全变化
        try:
            self._on_safety(controller.snapshot())
        except Exception:  # noqa: BLE001
            pass
        return True

    def detach(self, controller: Any) -> None:
        try:
            controller.remove_observer(self._on_safety)
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
        self._emit(EV_AGENT_STOPPED, dedupe_key=None)
        self._worker_stop.set()
        try:
            self._outbox.put_nowait(None)
        except queue.Full:
            pass
        worker = self._worker
        if worker is not None:
            worker.join(timeout=1.0)
            self._worker = None

    # -- 观察（回调在 SafetyController 锁外、且必须快） ----------------------

    def _on_safety(self, snap: Any) -> None:
        try:
            state = str(getattr(getattr(snap, "state", None), "value", "") or "")
            with self._lock:
                prev = self._last_state
                self._last_state = state
            self._translate(prev, state, snap)
        except Exception:  # noqa: BLE001 - 观察者绝不能把安全层拖挂
            pass

    def _translate(self, prev: str | None, state: str, snap: Any) -> None:
        emergency = state == "EMERGENCY_STOP"
        override = state == "HUMAN_OVERRIDE"
        injecting = state in _INJECTION_STATES

        # ---- 事件类型：只反映真实的状态迁移 --------------------------------
        if state == "EMERGENCY_STOP" and prev != "EMERGENCY_STOP":
            etype = EV_EMERGENCY_STOP
        elif injecting and prev not in _INJECTION_STATES:
            etype = EV_INPUT_CONTROL_ACQUIRED
        elif prev in _INJECTION_STATES and not injecting and state not in _TERMINAL_STATES:
            etype = EV_INPUT_CONTROL_RELEASED
        else:
            etype = EV_SAFETY_CHANGED

        # ---- 状态与 Attention ----------------------------------------------
        # 安全条目的 Status 直接复用同名词汇表（fail-closed 语义见 P0 报告）：
        #   IDLE / RELEASED      → IDLE   （键鼠在用户手里，一切正常）
        #   REQUESTED/BANNER/... → RUNNING（CU 生命周期推进中）
        #   CONTROL_ACQUIRED/ACTIVE → RUNNING + L5（输入已被 Agent 接管）
        #   EMERGENCY_STOP       → ERROR  + L5
        #   HUMAN_OVERRIDE       → PAUSED + L5（人已接管，等待显式恢复）
        if emergency:
            status, attention = Status.ERROR, Attention.L5
        elif override:
            status, attention = Status.PAUSED, Attention.L5
        elif injecting:
            status, attention = Status.RUNNING, Attention.L5
        elif state in ("IDLE", "RELEASED"):
            status, attention = Status.IDLE, Attention.L0
        else:
            status, attention = Status.RUNNING, Attention.L2

        hotkey = str(getattr(snap, "hotkey", "") or "ctrl+alt+f12")
        reason = getattr(snap, "emergency_reason", None)
        summary = self._summary_line(state, injecting, emergency, override, hotkey)
        # detail 里带 machine-readable 的 hotkey：快照线上格式没有 payload，
        # 所以把"要在 UI 上显示的真实热键"放进 detail（HUD 解析 hotkey=...）。
        detail_bits = [b for b in (str(reason) if reason else "", f"hotkey={hotkey}") if b]

        self._emit(
            etype,
            status=status,
            activity=Activity(summary=summary, detail=" · ".join(detail_bits),
                              tool="SafetyGate"),
            task=TaskState(name="键鼠安全", stage=state),
            payload={"safety_state": state, "hotkey": hotkey,
                     "injection_permission": bool(getattr(snap, "agent_injection_permission", False)),
                     "input_owner": str(getattr(getattr(snap, "input_owner", None), "value", "USER"))},
            dedupe_key=(etype, state, attention.value, str(reason) if reason else ""),
            attention=attention,
            attention_reason=summary,
        )

    @staticmethod
    def _summary_line(state: str, injecting: bool, emergency: bool, override: bool, hotkey: str) -> str:
        hk = hotkey.replace("+", " + ").upper()
        if emergency:
            return f"紧急停止已触发 · 恢复需人工确认（{hk}）"
        if override:
            return f"人工接管生效 · Agent 注入被拒绝（{hk}）"
        if injecting:
            return f"输入控制已接管 · 鼠标键鼠由 Agent 驱动（{hk} 紧急停止）"
        if state in ("REQUESTED", "BANNER_VISIBLE"):
            return "Computer Use 准备中 · 安全横幅前置"
        if state == "RELEASING":
            return "正在释放输入控制"
        if state == "RELEASED":
            return "输入控制已释放"
        return "安全层待机 · 键鼠在用户手里"

    @staticmethod
    def _hotkey_of(_snap: Any) -> str:
        return "ctrl+alt+f12"

    # -- 出站（与 UhaNativeAdapter 相同的非阻塞策略） ------------------------

    def _ensure_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker_stop.clear()
        self._worker = threading.Thread(target=self._drain_loop, name="uah-safety-bridge", daemon=True)
        self._worker.start()

    def _emit(
        self,
        etype: str,
        *,
        status: Status | None = None,
        activity: Activity | None = None,
        task: TaskState | None = None,
        payload: Mapping[str, Any] | None = None,
        dedupe_key: tuple | None = None,
        attention: Attention | None = None,
        attention_reason: str | None = None,
    ) -> None:
        with self._lock:
            if self._stopped:
                return
            if dedupe_key is not None:
                if dedupe_key == self._last_key:
                    return
                self._last_key = dedupe_key
            self._seq += 1
            seq = self._seq
        event: dict[str, Any] = {
            "protocol": PROTOCOL_VERSION,
            "event_id": new_event_id(),
            "seq": seq,
            "type": etype,
            "timestamp": time.time(),
            "agent": self.agent.to_wire(),
        }
        if status is not None:
            event["status"] = status.value
        if attention is not None:
            event["attention"] = int(attention.value)
            if attention_reason:
                event["attention_reason"] = attention_reason
        if task is not None and not task.is_empty:
            event["task"] = task.to_wire()
        if activity is not None and not activity.is_empty:
            event["activity"] = activity.to_wire()
        if payload:
            event["payload"] = dict(payload)
        self._ensure_worker()
        try:
            self._outbox.put_nowait(event)
        except queue.Full:
            try:
                self._outbox.get_nowait()
                self._outbox.task_done()
                self._outbox.put_nowait(event)
            except (queue.Empty, queue.Full):
                pass

    def _drain_loop(self) -> None:
        while True:
            try:
                item = self._outbox.get(timeout=0.2)
            except queue.Empty:
                if self._worker_stop.is_set():
                    return
                continue
            try:
                if item is None:
                    return
                self.publisher.publish(item)
            except Exception:  # noqa: BLE001 - 发布失败不重试也不崩
                pass
            finally:
                self._outbox.task_done()

    def flush(self, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._outbox.all_tasks_done:
            while self._outbox.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._outbox.all_tasks_done.wait(remaining)
        return True

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "agent": self.agent.to_wire(),
                "seq": self._seq,
                "last_state": self._last_state,
                "queued": self._outbox.qsize(),
                "stopped": self._stopped,
            }


class _NullPublisher:
    """无发布目标时的占位。"""

    def publish(self, event: Mapping[str, Any]) -> None:  # noqa: ARG002
        return None

    def delivered(self) -> dict[str, Any]:
        return {"mode": "null"}


__all__ = ["SafetyBridge", "SAFETY_AGENT_ID"]
