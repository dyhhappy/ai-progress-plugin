"""Computer Use 适配器 —— 对接 E:\\MCP\\Agent-TARS。

这是**唯一**负责与 Computer Use MCP 通信的地方。上层（skills / router）
永远不直接拼 tool 名字，只调用本文件暴露的方法。

设计要点：

* 走 **MCP over Streamable HTTP**（`http://127.0.0.1:8788/mcp`），不是 stdio。
  原因：Agent-TARS 的桌面锁是"进程内单例"，stdio 会一客户端一进程、
  锁各算各的，多 Agent 会互踩鼠标。HTTP 才能共享同一把锁。
* **不使用 `computer_do`**。它需要服务端配 UI-TARS 风格 VLM（本机未配），
  而且把"看"和"做"耦合在一起。本项目坚持：视觉判断由上层 `vision` 模块做，
  Computer Use 只当"手"。
* 只读工具（截图/尺寸/状态/等待）不抢锁；会动键鼠的工具必须持锁，
  由 `control()` 上下文管理器统一处理。
"""

from __future__ import annotations

import base64
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from ..core.errors import BackendUnavailable, DesktopBusy, ToolCallFailed, TransportError
from .mcp_client import StreamableHTTPTransport

# Agent-TARS 实际暴露的工具名 —— 与其 README §3 的工具清单一一对应
READ_ONLY_TOOLS = ("computer_screenshot", "computer_get_screen_size", "computer_wait", "computer_status")
LOCK_TOOLS = ("computer_acquire_control", "computer_release_control")
INPUT_TOOLS = (
    "computer_move_mouse",
    "computer_click",
    "computer_double_click",
    "computer_right_click",
    "computer_drag",
    "computer_scroll",
    "computer_type",
    "computer_hotkey",
    "computer_key_press",
    "computer_key_release",
)

# 本适配器刻意不注册的工具（存在但不用）
EXCLUDED_TOOLS = ("computer_do",)


@dataclass
class Screenshot:
    """一张截图。`image` 是 base64 PNG，`path` 是落盘位置（若要求保存）。"""

    width: int
    height: int
    image: str
    scale_factor: float = 1.0
    path: Path | None = None
    bytes_len: int = 0
    screen: dict[str, Any] | None = None

    def save(self, dest: str | Path) -> Path:
        p = Path(dest)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(base64.b64decode(self.image))
        self.path = p
        return p

    @property
    def aspect(self) -> float:
        return (self.width / self.height) if self.height else 0.0


@dataclass
class ScreenSize:
    width: int
    height: int
    scale_factor: float = 1.0
    physical_width: int = 0
    physical_height: int = 0

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ScreenSize":
        return cls(
            width=int(payload.get("width") or 0),
            height=int(payload.get("height") or 0),
            scale_factor=float(payload.get("scaleFactor") or 1.0),
            physical_width=int(payload.get("physicalWidth") or 0),
            physical_height=int(payload.get("physicalHeight") or 0),
        )


class ComputerUseAdapter:
    """Computer Use 后端。"""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8788",
        *,
        endpoint: str = "/mcp",
        token: str = "",
        client_id: str = "unreal-hybrid-agent",
        timeout: float = 120.0,
        dry_run: bool = False,
    ):
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.dry_run = dry_run
        self._client = StreamableHTTPTransport(
            self.base_url + endpoint, token=token, name="unreal-hybrid-agent", timeout=timeout
        )
        self._ready = False
        self._holds_lock = False
        self._info: dict[str, Any] = {}

    # -- 生命周期 -------------------------------------------------------------

    def connect(self, *, required: bool = True) -> dict[str, Any]:
        """建立（或复用）会话。

        **必须幂等**：Streamable HTTP 的会话是一次性握手的，对同一个会话
        再发一次 ``initialize``，服务端会回
        ``Invalid Request: Server already initialized``。
        Router 每一步都会探一次可用性，所以这里重复调用是常态而非例外。
        """
        if self._ready:
            return {"available": True, "reused": True, **self._info}
        try:
            info = self._client.initialize()
        except Exception as exc:
            if required:
                raise BackendUnavailable(
                    f"Computer Use MCP 不可达: {self.base_url} —— {exc}\n"
                    f"提示：确认 Agent-TARS 已启动 (node server/index.js --transport http)"
                ) from exc
            return {"available": False, "error": str(exc)}
        self._ready = True
        self._info = dict(info)
        return {"available": True, **info}

    def close(self) -> None:
        self._client.close()
        self._ready = False
        self._info = {}

    def __enter__(self) -> "ComputerUseAdapter":
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.close()
        return False

    def _ensure(self) -> None:
        """确保已握手。任何需要访问服务端的方法都要先经过它。

        踩过的坑：`tool_names()` / `capabilities()` 若绕过握手直接发
        `tools/list`，服务端会回 `Bad Request: Server not initialized`
        （Streamable HTTP 要求先 initialize 建立会话）。
        """
        if not self._ready:
            self.connect()

    # -- 探测 ----------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        return self._client.health()

    def tool_names(self, *, refresh: bool = False) -> list[str]:
        self._ensure()
        return self._client.tool_names(refresh=refresh)

    def capabilities(self) -> dict[str, Any]:
        """返回"哪些能力可用"的报告 —— Router 用它判断能否选 MOUSE / KEYBOARD。"""
        names = set(self.tool_names(refresh=True))
        missing = [t for t in (*READ_ONLY_TOOLS, *LOCK_TOOLS, *INPUT_TOOLS) if t not in names]
        return {
            "available": bool(names),
            "tools": sorted(names),
            "missing": missing,
            "count": len(names),
            "has_do": "computer_do" in names,
            "screen": self.get_screen_size().__dict__ if names else None,
        }

    # -- 底层调用 ------------------------------------------------------------

    def _call(self, tool: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if tool in EXCLUDED_TOOLS:
            raise ToolCallFailed(
                f"{tool} 被本项目禁用：视觉定位交由 vision 模块，Computer Use 只作为底层执行能力"
            )
        if not self._ready:
            self.connect()
        args = dict(arguments or {})
        if tool in (*INPUT_TOOLS, *LOCK_TOOLS) and "clientId" not in args:
            args["clientId"] = self.client_id

        try:
            result = self._client.call_tool(tool, args)
        except ToolCallFailed as exc:
            code = str((exc.details.get("rpc_error") or {}).get("message") or exc.message)
            if "DESKTOP_BUSY" in code:
                raise DesktopBusy(code, details=exc.details) from exc
            raise
        except TransportError:
            raise

        data = result.get("data") or {}
        if result.get("is_error") or (isinstance(data, Mapping) and data.get("ok") is False):
            err = data.get("error") if isinstance(data, Mapping) else None
            code = data.get("code") if isinstance(data, Mapping) else None
            msg = err or f"{tool} 返回失败"
            if code == "DESKTOP_BUSY":
                raise DesktopBusy(msg, details=data.get("details") or {})
            raise ToolCallFailed(msg, code=code or "TOOL_CALL_FAILED", details={"tool": tool, "args": args, "payload": data})
        return result

    # -- 只读能力 ------------------------------------------------------------

    def get_screen_size(self) -> ScreenSize:
        res = self._call("computer_get_screen_size")
        payload = res.get("data") or {}
        return ScreenSize.from_payload(payload.get("screen") or {})

    def status(self) -> dict[str, Any]:
        res = self._call("computer_status")
        return dict(res.get("data") or {})

    def wait(self, ms: int = 1000) -> dict[str, Any]:
        return dict(self._call("computer_wait", {"ms": int(ms)}).get("data") or {})

    def screenshot(self, *, max_width: int = 0, save_to: str | Path | None = None) -> Screenshot:
        args: dict[str, Any] = {}
        if max_width:
            args["maxWidth"] = int(max_width)
        res = self._call("computer_screenshot", args)
        payload = res.get("data") or {}
        meta = payload.get("screenshot") or {}
        images = res.get("images") or []
        if not images:
            raise ToolCallFailed("computer_screenshot 未返回 image 内容")
        shot = Screenshot(
            width=int(meta.get("width") or 0),
            height=int(meta.get("height") or 0),
            image=images[0]["data"],
            scale_factor=float(meta.get("scaleFactor") or 1.0),
            bytes_len=int(meta.get("bytes") or 0),
            screen=meta.get("screen"),
        )
        if save_to:
            shot.save(save_to)
        return shot

    # -- 锁控制 --------------------------------------------------------------

    def peek_lock(self) -> dict[str, Any]:
        return dict((self.status().get("lock") or {}))

    def acquire_control(self, *, wait: bool = False) -> dict[str, Any]:
        res = self._call("computer_acquire_control", {"clientId": self.client_id, "wait": bool(wait)})
        self._holds_lock = True
        return dict(res.get("data") or {})

    def release_control(self, *, force: bool = False) -> dict[str, Any]:
        res = self._call("computer_release_control", {"clientId": self.client_id, "force": bool(force)})
        self._holds_lock = False
        return dict(res.get("data") or {})

    @contextmanager
    def control(self, *, wait: bool = False, release: bool = True) -> Iterator[None]:
        """独占桌面的上下文管理器。

        ```python
        with cu.control(wait=True):
            cu.click(500, 300)
            cu.hotkey("ctrl s")
        ```
        未能取得锁时抛 `DesktopBusy`，由上层 fallback 决定重试还是换方法。
        """
        self.acquire_control(wait=wait)
        try:
            yield
        finally:
            if release:
                try:
                    self.release_control()
                except Exception:
                    # 释放失败不掩盖业务异常；服务端 TTL 会兜底
                    pass

    # -- 会动键鼠的能力 -------------------------------------------------------

    def move_mouse(self, x: int, y: int) -> dict[str, Any]:
        return dict(self._call("computer_move_mouse", {"x": int(x), "y": int(y)}).get("data") or {})

    def click(self, x: int, y: int) -> dict[str, Any]:
        return dict(self._call("computer_click", {"x": int(x), "y": int(y)}).get("data") or {})

    def double_click(self, x: int, y: int) -> dict[str, Any]:
        return dict(self._call("computer_double_click", {"x": int(x), "y": int(y)}).get("data") or {})

    def right_click(self, x: int, y: int) -> dict[str, Any]:
        return dict(self._call("computer_right_click", {"x": int(x), "y": int(y)}).get("data") or {})

    def drag(self, x1: int, y1: int, x2: int, y2: int) -> dict[str, Any]:
        return dict(
            self._call("computer_drag", {"x1": int(x1), "y1": int(y1), "x2": int(x2), "y2": int(y2)}).get("data") or {}
        )

    def scroll(self, x: int, y: int, *, direction: str = "down", times: int = 1) -> dict[str, Any]:
        return dict(
            self._call(
                "computer_scroll",
                {"x": int(x), "y": int(y), "direction": direction, "times": max(1, min(10, int(times)))},
            ).get("data")
            or {}
        )

    def type_text(self, text: str) -> dict[str, Any]:
        return dict(self._call("computer_type", {"text": str(text)}).get("data") or {})

    def hotkey(self, keys: str) -> dict[str, Any]:
        return dict(self._call("computer_hotkey", {"keys": str(keys)}).get("data") or {})

    def key_press(self, key: str) -> dict[str, Any]:
        return dict(self._call("computer_key_press", {"key": str(key)}).get("data") or {})

    def key_release(self, key: str) -> dict[str, Any]:
        return dict(self._call("computer_key_release", {"key": str(key)}).get("data") or {})

    # -- 组合动作（仍然是"手"，不含任何视觉判断） ----------------------------

    def click_text_field_then_type(self, x: int, y: int, text: str, *, settle_ms: int = 250) -> dict[str, Any]:
        self.click(x, y)
        self.wait(settle_ms)
        self.type_text(text)
        return {"clicked": [x, y], "typed": len(text)}

    def save_with_ctrl_s(
        self,
        *,
        focus_point: tuple[int, int] | None = None,
        settle_ms: int = 800,
    ) -> dict[str, Any]:
        """Ctrl+S —— MVP Demo 里"保存 Level"这条路径的执行动作。

        ``focus_point``：UE 编辑器窗口内任意一点的**逻辑像素**坐标。
        为什么需要它：Computer Use 没有"聚焦某个窗口"的工具，
        ``computer_hotkey`` 只会把按键发给**当前有焦点的窗口**。
        如果执行时焦点在别处（终端、浏览器），Ctrl+S 就是发给空气——
        实测验证器正是靠文件 mtime 抓到过这种情况。
        没有标定坐标时**不猜**：照常发快捷键，但如实记录未确保焦点。
        """
        focused = False
        if focus_point is not None:
            self.click(int(focus_point[0]), int(focus_point[1]))
            self.wait(250)
            focused = True
        self.hotkey("ctrl s")
        self.wait(settle_ms)
        return {
            "hotkey": "ctrl s",
            "settled_ms": settle_ms,
            "focused_first": focused,
            "focus_point": list(focus_point) if focus_point else None,
        }


def build_from_config(
    section: Mapping[str, Any],
    *,
    client_id: str = "unreal-hybrid-agent",
    dry_run: bool = False,
) -> ComputerUseAdapter:
    """从配置的 `mcp_servers.computer_use` 段构造适配器。"""
    transport = str(section.get("transport", "http")).lower()
    if transport == "stdio":
        raise BackendUnavailable(
            "Computer Use 必须使用 HTTP 传输：stdio 会导致每个客户端各持一把独立桌面锁，"
            "多 Agent 场景下会互踩鼠标键盘。"
        )
    base = str(section.get("base_url") or "").rstrip("/")
    if not base:
        raise BackendUnavailable("未配置 mcp_servers.computer_use.base_url")
    return ComputerUseAdapter(
        base,
        endpoint=str(section.get("endpoint") or "/mcp"),
        token=str(section.get("token") or ""),
        client_id=client_id,
        timeout=float(section.get("request_timeout_s") or 120),
        dry_run=dry_run,
    )


__all__ = [
    "ComputerUseAdapter",
    "Screenshot",
    "ScreenSize",
    "build_from_config",
    "READ_ONLY_TOOLS",
    "LOCK_TOOLS",
    "INPUT_TOOLS",
    "EXCLUDED_TOOLS",
]
