"""本机传输层 —— 127.0.0.1 上的 HTTP + SSE Hub。

为什么是 HTTP + SSE，而不是 WebSocket / Named Pipe（架构文档 §10.1 有完整论证）：

* **零依赖**。stdlib 的 ``http.server`` + ``urllib`` 就够。本机没有 ``websockets`` 包，
  手写 RFC6455 握手与分帧是纯风险、零收益。
* **SSE 是事件流不是轮询**。服务端 push，客户端阻塞读一行。空闲时 CPU 为 0。
* **崩溃天然隔离**。Agent 死 = 连接 EOF，HUD 只看到断流，不会跟着死。
* **重连天然成立**。``GET /state`` 拿全量快照，服务端不需要保存会话。
* **多 Agent 天然成立**。多路 POST 汇聚到同一个 ``StateStore``。
* **不过度设计**。没有 Redis / 数据库 / MQ —— 需求 §八 明令禁止。

端点：

    GET  /                自述
    GET  /health          存活 + 协议版本 + Agent 计数
    GET  /state           全量快照（HUD 重启后靠它立刻恢复）
    GET  /stream          SSE 流：先全量，再增量
    POST /event           摄入一条事件（Agent 侧入口，也是 Generic Adapter 入口）

"先到先得"的 Hub 选举（``ensure_hub``）：谁先起来谁在默认端口上把 Hub 跑起来；
后起的发现端口已被占用就只当客户端。**用户不需要手动启动任何服务。**
"""

from __future__ import annotations

import errno
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping

from .events import EventBus
from .models import AgentSnapshot, Event
from .protocol import (
    DEFAULT_HUB_HOST,
    DEFAULT_HUB_PORT,
    HEARTBEAT_INTERVAL_S,
    PROTOCOL_VERSION,
    UAH_CORE_VERSION,
    protocol_compatible,
)
from .state import DEFAULT_STALE_AFTER_S, StateStore

SnapshotHandler = Callable[[AgentSnapshot], None]
RemovedHandler = Callable[[str], None]

#: 客户端读超时。必须大于服务端心跳间隔，否则自己会误判"服务端死了"。
READ_TIMEOUT_S = HEARTBEAT_INTERVAL_S * 2.5


# ---------------------------------------------------------------------------
# 服务端
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server_version = "UAHHub/" + UAH_CORE_VERSION
    #: 用 HTTP/1.0：SSE 这种"无 Content-Length、写不完就不断开"的流，
    #: 在 1.0 语义下（读到 EOF 为止）是天然正确的；1.1 反而要求 chunked。
    protocol_version = "HTTP/1.0"

    # -- 基础 ---------------------------------------------------------------

    def handle(self) -> None:
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError):
            # Expected for process kills and interrupted SSE clients.
            pass

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        hub: "HubServer" = self.server.hub  # type: ignore[attr-defined]
        if hub.verbose:
            hub._log(f"http {self.address_string()} {fmt % args}")

    def _send_json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        return self.rfile.read(length)

    # -- 路由 ---------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        hub: "HubServer" = self.server.hub  # type: ignore[attr-defined]
        if path == "/":
            self._send_json(200, hub.describe())
        elif path == "/health":
            self._send_json(200, hub.health())
        elif path == "/state":
            self._send_json(200, {
                "protocol": PROTOCOL_VERSION,
                "revision": hub.store.revision,
                "agents": hub.store.wire_snapshots(),
                "stats": hub.store.stats(),
            })
        elif path == "/timeline":
            try:
                limit = int(self.path.split("?", 1)[-1].split("&")[0].split("=")[-1]) if "?" in self.path else 50
            except ValueError:
                limit = 50
            agent = None
            if "agent=" in self.path:
                agent = self.path.split("agent=")[-1].split("&")[0] or None
            self._send_json(200, {
                "protocol": PROTOCOL_VERSION,
                "events": hub.timeline(agent, limit=min(max(limit, 1), 200)),
            })
        elif path == "/control/result":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            self._send_json(200, hub.control_result(query.get("id", [""])[0]))
        elif path == "/control/pending":
            if not hub.verify_capability(self.headers.get("X-UAH-Approval", "")):
                self._send_json(403, {"ok": False, "error": "capability required"})
                return
            wait_s = 0.0
            if "wait=" in self.path:
                try:
                    wait_s = min(float(self.path.split("wait=")[-1].split("&")[0]), 15.0)
                except ValueError:
                    wait_s = 0.0
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            cmds = hub.take_control_commands(wait_s=wait_s, agent_id=query.get("agent", ["uha"])[0])
            self._send_json(200, {"ok": True, "commands": cmds})
        elif path == "/stream":
            self._stream()
        else:
            self._send_json(404, {"error": "not found", "path": path})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        hub: "HubServer" = self.server.hub  # type: ignore[attr-defined]
        if path.startswith("/approval/"):
            import secrets
            if (self.headers.get("Origin") or self.client_address[0] not in ("127.0.0.1", "::1")
                    or not hub.approval_token or not secrets.compare_digest(self.headers.get("X-UAH-Approval", ""), hub.approval_token)):
                self._send_json(403, {"ok": False, "error": "local approval capability required"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16384: raise ValueError("invalid length")
                data = json.loads(self.rfile.read(length))
                result = hub.approvals.handle(path.rsplit("/", 1)[-1], data)
                self._send_json(200, result)
            except Exception:
                self._send_json(400, {"ok": False, "error": "invalid approval request"})
            return
        if path == "/control/result":
            if self.headers.get("Origin") or not hub.verify_capability(self.headers.get("X-UAH-Approval", "")):
                self._send_json(403, {"ok": False, "error": "capability required"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise ValueError("invalid length")
                data = json.loads(self.rfile.read(length))
                self._send_json(200, hub.finish_control(data))
            except (ValueError, TypeError):
                self._send_json(400, {"ok": False, "error": "invalid result"})
            return
        if path == "/control":
            if not hub.verify_capability(self.headers.get("X-UAH-Approval", "")):
                self._send_json(403, {"ok": False, "error": "capability required"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise ValueError("invalid length")
                data = json.loads(self.rfile.read(length))
            except Exception:
                self._send_json(400, {"ok": False, "error": "invalid control request"})
                return
            result = hub.submit_control(
                str(data.get("action", "")),
                agent_id=str(data.get("agent") or "uha"),
                source=str(data.get("source") or "hud"),
            )
            self._send_json(200 if result.get("ok") else 400, result)
            return
        if path not in ("/event", "/events"):
            self._send_json(404, {"error": "not found", "path": path})
            return
        try:
            raw = self._read_body()
        except Exception as exc:  # noqa: BLE001
            self._send_json(400, {"error": f"read failed: {exc}"})
            return
        if not raw:
            self._send_json(400, {"error": "empty body"})
            return
        try:
            parsed = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            # 非法 JSON 也返回 200 + accepted=false：不是 5xx，不让客户端以为服务挂了
            self._send_json(200, {"ok": True, "accepted": False, "reason": f"invalid json: {exc}"})
            return

        if isinstance(parsed, list):
            payloads = parsed
        elif isinstance(parsed, Mapping) and isinstance(parsed.get("events"), list):
            payloads = list(parsed["events"])
        else:
            payloads = [parsed]

        results = []
        for item in payloads:
            out = hub.ingest(item)
            results.append({
                "accepted": out.accepted,
                "duplicate": out.duplicate,
                "stale_seq": out.stale_seq,
                "status_changed": out.status_changed,
                "status": out.snapshot.status.value if out.snapshot else None,
                "agent_id": out.snapshot.agent.id if out.snapshot else None,
                "reason": out.reason,
            })
        if len(results) == 1:
            self._send_json(200, {"ok": True, **results[0]})
        else:
            self._send_json(200, {"ok": True, "count": len(results), "results": results})

    # -- SSE ----------------------------------------------------------------

    def _stream(self) -> None:
        hub: "HubServer" = self.server.hub  # type: ignore[attr-defined]
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

        hub.note_subscriber(+1)
        sent: dict[str, tuple] = {}
        try:
            self._sse_send("hello", {
                "protocol": PROTOCOL_VERSION,
                "core": UAH_CORE_VERSION,
                "heartbeat_s": HEARTBEAT_INTERVAL_S,
            })
            # 1) 先全量：HUD 重启后立刻拿到当前状态，不需要"等下一次变化"
            #    签名与内容必须来自**同一趟** wire_snapshots()，否则会在
            #    "接入瞬间刚好有事件"时吞掉一条更新（见 state.wire_snapshots 的说明）。
            for row in hub.store.wire_snapshots():
                self._sse_send("snapshot", row)
                sent[str(row.get("agent", {}).get("id"))] = hub.store.signature_of(row)

            # 2) 再增量：等条件变量，不轮询
            while not hub.is_stopping():
                woke = hub.wait_for_change(timeout=1.0)
                if not woke:
                    self._sse_send(None, None)  # Heartbeat also advances staleness deadlines.
                    # No event arrives when an Agent is killed. Re-evaluate the
                    # local deadline and emit stale/removed snapshots on this tick.
                rows = hub.store.wire_snapshots()
                current_ids = set()
                for row in rows:
                    aid = str(row.get("agent", {}).get("id"))
                    current_ids.add(aid)
                    sig = hub.store.signature_of(row)
                    if sent.get(aid) != sig:
                        self._sse_send("snapshot", row)
                        sent[aid] = sig
                for aid in list(sent):
                    if aid not in current_ids:
                        self._sse_send("removed", {"agent_id": aid})
                        sent.pop(aid, None)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError, ValueError):
            pass
        finally:
            hub.note_subscriber(-1)

    def _sse_send(self, event: str | None, payload: Any) -> None:
        if event is None:
            chunk = f": ping {time.time():.3f}\n\n"
        else:
            data = json.dumps(payload, ensure_ascii=False, default=str)
            chunk = f"event: {event}\ndata: {data}\n\n"
        self.wfile.write(chunk.encode("utf-8"))
        self.wfile.flush()


class HubServer:
    """本机 Hub：持有唯一 ``StateStore``，对外提供摄入与订阅。"""

    def __init__(
        self,
        *,
        host: str = DEFAULT_HUB_HOST,
        port: int = DEFAULT_HUB_PORT,
        store: StateStore | None = None,
        bus: EventBus | None = None,
        verbose: bool = False,
    ) -> None:
        from .approval import ApprovalBroker
        self.approvals = ApprovalBroker()
        self.approval_token = ""
        self.host = host
        self.port = int(port)
        # 必须用 `is None` 而不是 `or`：StateStore 定义了 __len__，
        # **空 store 是 falsy**，`store or StateStore(...)` 会把调用方注入的
        # store 悄悄换成另一个 —— 于是"注入的 store"与"Hub 真正在用的 store"
        # 是两个对象，状态看起来对、其实各记各的。这类 bug 不会报错，只会对不上。
        self.store = store if store is not None else StateStore(stale_after_s=DEFAULT_STALE_AFTER_S)
        self.bus = bus or EventBus()
        self.verbose = verbose
        self.started_at = time.time()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._cond = threading.Condition()
        self._stopping = False
        self._subscribers = 0
        self._ingested = 0
        self._rejected = 0
        self._log_lines: list[str] = []
        # --- UAH v2 -----------------------------------------------------------
        #: 每 Agent 的事件时间线（真实事件流落账，Expanded 的"最近活动"数据源）。
        self._timeline: dict[str, list[dict[str, Any]]] = {}
        self._timeline_cap = 200
        #: HUD → Agent 的控制命令队列（pause/resume/stop；不含急停——急停只走热键）。
        self._pending_control: list[dict[str, Any]] = []
        self._control_results: dict[str, dict[str, Any]] = {}
        self._control_cond = threading.Condition()

    # -- 生命周期 -----------------------------------------------------------

    def start(self, *, block: bool = False) -> "HubServer":
        if self._httpd is not None:
            return self
        httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        httpd.daemon_threads = True
        # 绝不能让一个"卡住的订阅者"拖住 Hub 的关闭：
        # ThreadingMixIn 默认会在 server_close() 里 join 它起过的**所有**线程
        # （包括 daemon 线程）。SSE 的连接本来就是长连接，HUD 被 kill 时那条
        # 线程可能正停在写 socket 上 —— 关 Hub 就会一直等它。
        # 关掉 block_on_close 后，Hub 的退出变成确定性的。
        httpd.block_on_close = False
        httpd.hub = self  # type: ignore[attr-defined]
        self._httpd = httpd
        # 端口传 0 时由系统分配，把真实端口记回来
        self.port = httpd.server_address[1]
        if self.host in ("127.0.0.1", "localhost", "::1"):
            import secrets
            from .approval import token_path
            self.approval_token = secrets.token_hex(32)
            path = token_path(self.url())
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self.approval_token, encoding="utf-8")
            try: path.chmod(0o600)
            except OSError: pass
        self._thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2},
                                        name="uah-hub", daemon=True)
        self._thread.start()
        self._log(f"hub listening on http://{self.host}:{self.port}")
        if block:
            try:
                while not self._stopping:
                    time.sleep(0.2)
            except KeyboardInterrupt:
                pass
            finally:
                self.stop()
        return self

    def stop(self) -> None:
        self._stopping = True
        with self._cond:
            self._cond.notify_all()
        httpd = self._httpd
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:  # noqa: BLE001
                pass
            try:
                httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._httpd = None
        self._thread = None

    def is_stopping(self) -> bool:
        return self._stopping

    # -- 条件变量（SSE 不轮询的落点） ---------------------------------------

    def notify_change(self) -> None:
        with self._cond:
            self._cond.notify_all()

    def wait_for_change(self, timeout: float) -> bool:
        with self._cond:
            return bool(self._cond.wait(timeout=timeout))

    # -- 摄入 ---------------------------------------------------------------

    def ingest(self, raw: object) -> Any:
        """摄入一条事件：写状态 + 广播 + 通知 SSE + 时间线落账。"""
        out = self.store.apply(raw)
        if out.accepted:
            self._ingested += 1
        else:
            self._rejected += 1
        if out.accepted and not out.duplicate and not out.stale_seq:
            self._record_timeline(out)
            self.bus.publish(out.snapshot)
            self.notify_change()
        return out

    def _record_timeline(self, out: Any) -> None:
        """把一条真实事件写进该 Agent 的时间线。

        文本只来自事件本身携带的信息（事件类型 + 快照当前 activity/status），
        绝不编造「正在优化……」这类没有事件依据的文案（需求 §十一）。
        """
        try:
            snap = out.snapshot
            if snap is None:
                return
            aid = snap.agent.id
            etype = snap.last_event_type or "event"
            # agent.stopped / 心跳不进时间线：前者是离线标记，后者是噪音
            if etype in ("agent.heartbeat",):
                return
            text = self._timeline_text(out, snap, etype)
            import time as _t

            with self._cond:  # 复用同一把条件锁保护 timeline（轻量、低频）
                bucket = self._timeline.setdefault(aid, [])
                bucket.append({
                    "ts": _t.time(),
                    "type": etype,
                    "status": snap.status.value,
                    "attention": int(snap.attention.value),
                    "text": text,
                })
                if len(bucket) > self._timeline_cap:
                    del bucket[: len(bucket) - self._timeline_cap]
        except Exception:  # noqa: BLE001 - 时间线故障不能影响主状态机
            pass

    @staticmethod
    def _timeline_text(out: Any, snap: Any, etype: str) -> str:
        act = snap.activity.one_line if snap.activity else ""
        task_name = snap.task.name if snap.task else None
        prefix = {
            "task.started": "收到任务",
            "task.completed": "任务完成",
            "task.failed": "任务失败",
            "task.cancelled": "任务取消",
            "task.paused": "已暂停",
            "task.resumed": "已恢复",
            "task.stop_requested": "收到停止请求",
            "task.step_changed": "进入下一步",
            "verification.started": "开始验证",
            "verification.passed": "验证通过",
            "verification.failed": "验证未通过",
            "approval.required": "等待批准",
            "user_input.required": "等待输入",
            "agent.started": "Agent 上线",
            "agent.stopped": "Agent 退出",
            "safety.emergency_stop": "紧急停止",
            "input.control_acquired": "接管输入控制",
            "input.control_released": "释放输入控制",
            "safety.changed": "安全状态变化",
        }.get(etype, "")
        parts = [b for b in (prefix, act) if b]
        if not parts:
            # 兜底：状态变化类事件给「状态：X」；其他给事件类型本身（不编故事）
            if etype in ("status.changed", "activity.changed", "task.stage_changed"):
                parts = [act or f"{snap.status.value}"]
            else:
                parts = [etype]
        if task_name and etype in ("task.started", "task.completed", "task.failed"):
            parts.append(task_name)
        tool = snap.activity.tool if snap.activity else None
        if tool and etype in ("task.stage_changed", "status.changed"):
            parts.append(f"[{tool}]")
        return " · ".join(parts)

    def timeline(self, agent_id: str | None = None, *, limit: int = 50) -> list[dict[str, Any]]:
        """读时间线。``agent_id`` 为空返回全部（按时间倒序，新的在前）。"""
        with self._cond:
            if agent_id:
                rows = list(self._timeline.get(agent_id, ()))
            else:
                rows = [r for bucket in self._timeline.values() for r in bucket]
        rows.sort(key=lambda r: r.get("ts", 0.0), reverse=True)
        return rows[: max(1, int(limit))]

    # -- HUD → Agent 控制命令 ------------------------------------------------

    def submit_control(self, action: str, *, agent_id: str = "uha", source: str = "hud") -> dict[str, Any]:
        """HUD 请求暂停/恢复/停止。命令**排队**，由 UHA 进程内的轮询端取走执行。

        这里刻意**不**支持 emergency_stop：急停是热键 + SafetyController 的职责，
        HUD 只是可视化层（需求 §十四：HUD 不能成为安全机制）。
        """
        act = str(action or "").strip().lower()
        if act not in ("pause", "resume", "stop"):
            return {"ok": False, "error": f"unsupported action: {action!r}"}
        import uuid
        cmd = {"id": uuid.uuid4().hex, "action": act, "agent": agent_id,
               "source": source, "ts": time.time(), "expires_at": time.time()+8}
        with self._control_cond:
            self._prune_controls()
            if len(self._pending_control) >= 128:
                return {"ok": False, "error": "control queue full"}
            self._pending_control.append(cmd)
            self._control_results[cmd["id"]] = {**cmd, "state": "queued"}
            self._control_cond.notify_all()
        return {"ok": True, "queued": act, "id": cmd["id"]}

    def _prune_controls(self):
        now = time.time()
        for result in self._control_results.values():
            if result["state"] in ("queued", "delivered") and now >= result["expires_at"]:
                result.update(state="expired", message="请求超时，未确认生效")
        self._pending_control = [c for c in self._pending_control if c["expires_at"] > now]
        for key in list(self._control_results):
            if now - self._control_results[key]["ts"] > 120:
                del self._control_results[key]

    def control_result(self, command_id):
        with self._control_cond:
            self._prune_controls()
            return dict(self._control_results.get(command_id, {"state": "unknown"}))

    def finish_control(self, data):
        with self._control_cond:
            self._prune_controls()
            result = self._control_results.get(data.get("id"))
            if not result or result["state"] != "delivered" or result["agent"] != data.get("agent"):
                return {"ok": False, "error": "unknown, expired or already acknowledged"}
            result.update(state="applied" if data.get("applied") is True else "failed",
                          message=str(data.get("message") or "")[:200])
            return {"ok": True}

    def take_control_commands(self, *, wait_s=0.0, agent_id="uha"):
        deadline = time.monotonic() + max(0, wait_s)
        with self._control_cond:
            while True:
                self._prune_controls()
                cmds = [c for c in self._pending_control if c["agent"] == agent_id]
                if cmds:
                    self._pending_control = [c for c in self._pending_control if c["agent"] != agent_id]
                    for cmd in cmds:
                        self._control_results[cmd["id"]]["state"] = "delivered"
                    return cmds
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._control_cond.wait(timeout=min(remaining, 0.5))

    def emit(self, event: Event | Mapping[str, Any]) -> dict[str, Any]:
        """本地直接发布（Agent 在自己的进程里跑 Hub 时用，省一次 HTTP 往返）。"""
        out = self.ingest(event)
        return {
            "accepted": out.accepted,
            "duplicate": out.duplicate,
            "status": out.snapshot.status.value if out.snapshot else None,
            "reason": out.reason,
        }

    # -- 观察 ---------------------------------------------------------------

    def verify_capability(self, token: str) -> bool:
        """校验本地能力令牌（审批与控制命令共用同一份）。

        只信任 127.0.0.1 + 令牌：别的机器连不上，本机没令牌也调不动控制命令。
        """
        import secrets

        if not self.approval_token:
            return False
        return secrets.compare_digest(str(token or ""), self.approval_token)

    def note_subscriber(self, delta: int) -> None:
        self._subscribers = max(0, self._subscribers + delta)

    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def describe(self) -> dict[str, Any]:
        return {
            "protocol": PROTOCOL_VERSION,
            "core": UAH_CORE_VERSION,
            "url": self.url(),
            "endpoints": {
                "health": "GET /health",
                "state": "GET /state",
                "stream": "GET /stream",
                "event": "POST /event",
            },
            "store": self.store.stats(),
        }

    def health(self) -> dict[str, Any]:
        import os
        return {
            "pid": os.getpid(),
            "ok": True,
            "protocol": PROTOCOL_VERSION,
            "protocol_ok": protocol_compatible(PROTOCOL_VERSION),
            "core": UAH_CORE_VERSION,
            "uptime_s": round(time.time() - self.started_at, 3),
            "agents": len(self.store),
            "subscribers": self._subscribers,
            "ingested": self._ingested,
            "rejected": self._rejected,
            "revision": self.store.revision,
        }

    def _log(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} [uah-hub] {msg}"
        self._log_lines.append(line)
        del self._log_lines[:-200]
        if self.verbose:
            print(line, flush=True)

    def log_lines(self) -> list[str]:
        return list(self._log_lines)


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class HubClient:
    """Hub 的客户端。只用 ``urllib``，因此 3.12 / 3.13 都能跑。"""

    def __init__(self, url: str, *, timeout_s: float = 5.0) -> None:
        self.url = url.rstrip("/")
        self.timeout_s = float(timeout_s)
        self._lock = threading.Lock()
        #: 当前正在读的 SSE 连接对应的 **socket**（不是 HTTPResponse）。见 close()。
        self._active_sock: Any = None

    def close(self) -> None:
        """主动断开流。可在别的线程里调用（HUD 关窗口时就是这么用的）。

        ★ 这里**只能 shutdown socket，绝对不能 close HTTPResponse**。

        实测踩过的坑（用 faulthandler 抓到的死锁）：

            close() → http.client.HTTPResponse.close() → _close_conn()
                    → io.BufferedReader.close()   ← 卡在这里

        ``HTTPResponse.close()`` 会去关 socket 上那层**缓冲文件对象**，
        而缓冲区的内部锁此刻正被另一个线程的阻塞 ``readinto()`` 持有 ——
        于是主线程关窗口永久卡住。Windows 上尤其稳定复现。

        ``socket.shutdown(SHUT_RDWR)`` 则不需要那把锁：它直接让阻塞中的
        ``recv`` 返回，读线程随即自己走完 ``with resp:`` 的收尾路径。
        """
        with self._lock:
            sock = self._active_sock
            self._active_sock = None
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    # -- 单次请求 -----------------------------------------------------------

    def _get(self, path: str, timeout: float | None = None) -> Any:
        req = urllib.request.Request(self.url + path, method="GET")
        with urllib.request.urlopen(req, timeout=timeout or self.timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    def _post(self, path: str, payload: Any, timeout: float | None = None) -> Any:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        req = urllib.request.Request(
            self.url + path, data=body, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with urllib.request.urlopen(req, timeout=timeout or self.timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    def health(self, timeout: float | None = None) -> dict[str, Any] | None:
        try:
            return self._get("/health", timeout)
        except Exception:  # noqa: BLE001 - 探活失败就是"不在"，不是异常
            return None

    def is_alive(self, timeout: float = 0.6) -> bool:
        h = self.health(timeout=timeout)
        return bool(h and h.get("ok"))

    def state(self, timeout: float | None = None) -> list[AgentSnapshot]:
        try:
            payload = self._get("/state", timeout)
        except Exception:  # noqa: BLE001
            return []
        raw = payload.get("agents") if isinstance(payload, Mapping) else None
        if not isinstance(raw, list):
            return []
        out: list[AgentSnapshot] = []
        for item in raw:
            try:
                out.append(AgentSnapshot.from_wire(item))
            except Exception:  # noqa: BLE001 - 一条坏快照不该毁掉整份列表
                continue
        return out

    def post_event(self, payload: Any, timeout: float | None = None) -> dict[str, Any]:
        try:
            res = self._post("/event", payload, timeout)
            return res if isinstance(res, dict) else {"ok": True, "raw": res}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def timeline(self, agent_id: str | None = None, *,
                 limit: int = 50, timeout: float | None = None) -> list[dict[str, Any]]:
        """读事件时间线（HUD 的「最近活动」数据源）。失败返回空列表。"""
        query = f"?limit={int(limit)}"
        if agent_id:
            query += f"&agent={urllib.parse.quote(str(agent_id))}"
        try:
            payload = self._get("/timeline" + query, timeout or min(self.timeout_s, 1.5))
        except Exception:  # noqa: BLE001
            return []
        rows = payload.get("events") if isinstance(payload, Mapping) else None
        return rows if isinstance(rows, list) else []

    def submit_control(self, action: str, *, agent_id: str = "uha",
                       token: str | None = None, timeout: float | None = None) -> dict[str, Any]:
        """提交软控制命令（pause/resume/stop）。需要能力令牌。"""
        body = json.dumps({"action": action, "agent": agent_id}).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["X-UAH-Approval"] = token
        req = urllib.request.Request(self.url + "/control", data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout or 2.0) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            return data if isinstance(data, dict) else {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # -- 流 -----------------------------------------------------------------

    def stream(
        self,
        on_snapshot: SnapshotHandler,
        *,
        on_removed: RemovedHandler | None = None,
        on_status: Callable[[str], None] | None = None,
        stop: threading.Event | None = None,
        reconnect: bool = True,
        reconnect_delay_s: float = 1.0,
        read_timeout_s: float = READ_TIMEOUT_S,
    ) -> str:
        """阻塞地读 SSE 流，直到 ``stop`` 被置位。

        返回停止原因：``"stopped"`` / ``"closed"``。
        断线**不抛异常**：自动重连（指数退避，最长 5s）。这就是"Agent 崩了 UI 不崩"。

        重连时会重新收到一次全量快照，所以调用方不需要自己记状态——
        每次 ``on_snapshot`` 都是权威值。
        """
        delay = reconnect_delay_s
        while True:
            if stop is not None and stop.is_set():
                return "stopped"
            try:
                req = urllib.request.Request(self.url + "/stream", method="GET")
                resp = urllib.request.urlopen(req, timeout=read_timeout_s)
            except Exception as exc:  # noqa: BLE001
                if on_status:
                    on_status(f"connect failed: {type(exc).__name__}: {exc}")
                if not reconnect:
                    return "closed"
                if stop is not None and stop.wait(delay):
                    return "stopped"
                delay = min(delay * 2, 5.0)
                continue
            delay = reconnect_delay_s
            if on_status:
                on_status("connected")
            with self._lock:
                self._active_sock = _socket_of(resp)
            try:
                with resp:
                    self._consume(resp, on_snapshot, on_removed, stop)
            except Exception as exc:  # noqa: BLE001
                if on_status:
                    on_status(f"stream error: {type(exc).__name__}: {exc}")
            finally:
                with self._lock:
                    self._active_sock = None
            if on_status:
                on_status("disconnected")
            if not reconnect:
                return "closed"
            if stop is not None and stop.wait(reconnect_delay_s):
                return "stopped"

    def _consume(
        self,
        resp: Any,
        on_snapshot: SnapshotHandler,
        on_removed: RemovedHandler | None,
        stop: threading.Event | None,
    ) -> None:
        event_name = "message"
        data_lines: list[str] = []
        while True:
            if stop is not None and stop.is_set():
                return
            raw = resp.readline()
            if not raw:
                return  # EOF = 服务端走了
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                if data_lines:
                    self._dispatch(event_name, "\n".join(data_lines), on_snapshot, on_removed)
                event_name, data_lines = "message", []
                continue
            if line.startswith(":"):
                continue
            if ":" in line:
                field, _, value = line.partition(":")
                value = value[1:] if value.startswith(" ") else value
            else:
                field, value = line, ""
            if field == "event":
                event_name = value.strip()
            elif field == "data":
                data_lines.append(value)

    @staticmethod
    def _dispatch(
        event_name: str,
        data: str,
        on_snapshot: SnapshotHandler,
        on_removed: RemovedHandler | None,
    ) -> None:
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            return
        try:
            if event_name == "snapshot":
                on_snapshot(AgentSnapshot.from_wire(payload))
            elif event_name == "removed" and on_removed is not None:
                aid = payload.get("agent_id") if isinstance(payload, Mapping) else None
                if aid:
                    on_removed(str(aid))
        except Exception:  # noqa: BLE001 - 一条坏快照不该让流断掉
            pass


# ---------------------------------------------------------------------------
# Hub 选举
# ---------------------------------------------------------------------------


def hub_url(host: str = DEFAULT_HUB_HOST, port: int = DEFAULT_HUB_PORT) -> str:
    return f"http://{host}:{int(port)}"


def probe_hub(url: str, *, timeout: float = 0.8) -> dict[str, Any] | None:
    h = HubClient(url, timeout_s=timeout).health(timeout=timeout)
    if h and h.get("ok") and protocol_compatible(h.get("protocol")):
        return h
    return None


def ensure_hub(
    *,
    host: str = DEFAULT_HUB_HOST,
    port: int = DEFAULT_HUB_PORT,
    store: StateStore | None = None,
    verbose: bool = False,
) -> tuple[HubServer | None, str]:
    """确保本机有一个 UAH Hub 可用。

    返回 ``(server_or_None, url)``：
      * 已有 Hub → ``(None, url)``（只当客户端，不抢端口）
      * 没有 → ``(server, url)``（自己起一个）
      * 端口被非 UAH 服务占了 → 抛 ``OSError``，信息里点明端口

    刻意不做"自动换端口"：换端口会让 HUD 连错地方，比直接报错更难排查。
    """
    url = hub_url(host, port)
    if probe_hub(url) is not None:
        return None, url

    server = HubServer(host=host, port=port, store=store, verbose=verbose)
    try:
        server.start()
    except OSError as exc:
        if exc.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", 10048), 10048):
            # 竞态：我们探活之后、绑定之前，别人先起来了
            if probe_hub(url) is not None:
                return None, url
            raise OSError(
                f"端口 {port} 已被占用，且上面的服务不是 UAH Hub。"
                f"请释放该端口，或用 --port 指定另一个端口。"
            ) from exc
        raise
    return server, url


def pick_free_port() -> int:
    """拿一个当前空闲的本机端口（测试用）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((DEFAULT_HUB_HOST, 0))
        return int(s.getsockname()[1])


def new_event_id() -> str:
    return uuid.uuid4().hex


def _socket_of(resp: Any) -> Any:
    """从 ``HTTPResponse`` 里挖出底层 socket（挖不到返回 None）。

    为了 ``HubClient.close()`` 能 ``shutdown`` 它。路径是
    ``resp.fp``（BufferedReader）→ ``.raw``（SocketIO）→ ``._sock``。
    这是 ``http.client`` 的实现细节，所以每一层都容错：
    挖不到就返回 None，``close()`` 退化成"什么都不做"，
    最坏情况只是要等一个读超时（37.5s），而不会卡死。
    """
    for getter in (lambda r: r.fp.raw._sock, lambda r: r.fp.fp.raw._sock, lambda r: r.fp.raw, lambda r: r.fp):
        try:
            candidate = getter(resp)
        except Exception:  # noqa: BLE001
            continue
        if candidate is not None and hasattr(candidate, "shutdown"):
            return candidate
    return None


__all__ = [
    "HubServer",
    "HubClient",
    "hub_url",
    "probe_hub",
    "ensure_hub",
    "pick_free_port",
    "new_event_id",
    "READ_TIMEOUT_S",
]
