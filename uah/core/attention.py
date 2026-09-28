"""Attention Level —— 「用户现在需不需要关注」。

Agent State（Status）回答「机器正在干什么」；
Attention Level 回答「这件事值不值得打断用户」。

两者必须是**独立概念**（需求 §六）：
同一个 Status 在不同上下文里可以对应不同 Attention，
所以这里既提供 Status → Attention 的**默认推导**，
也允许事件用 ``payload["attention"]`` 显式覆盖（例如安全层直接指定 L5）。

设计约束：

* Attention 是 0..5 的整数（L0..L5），**不进入 Status 枚举**——
  那会把两个正交维度搅在一起，重蹈「ERROR=红色 / RUNNING=蓝色」的覆辙。
* HUD 的排序、Alert 横幅的触发条件都只看这个数值。
* 默认推导故意保守：RUNNING/THINKING=L0（正常工作不打扰），
  只有真的需要人的时候才升到 L3+。
"""

from __future__ import annotations

import enum


class Attention(int, enum.Enum):
    """L0 无需关注 → L5 安全紧急。"""

    L0 = 0  # 无需关注（Agent 正常工作/待机）
    L1 = 1  # 一般状态变化（完成/取消等，值得知道，不必打断）
    L2 = 2  # 建议查看（暂停、被取消等异常但可等）
    L3 = 3  # 等待用户确认（WAITING_INPUT / WAITING_APPROVAL）
    L4 = 4  # 必须人工处理（ERROR / BLOCKED）
    L5 = 5  # 安全紧急（急停、输入控制未释放）

    def __int__(self) -> int:  # enum.IntEnum 语义
        return int(self.value)

    @property
    def label(self) -> str:
        return _LABELS.get(self, f"L{int(self.value)}")

    @property
    def is_alert(self) -> bool:
        """>= L4 需要横幅级提示。"""
        return self.value >= 4

    @property
    def is_safety(self) -> bool:
        return self is Attention.L5


_LABELS = {
    Attention.L0: "无需关注",
    Attention.L1: "状态变化",
    Attention.L2: "建议查看",
    Attention.L3: "等待确认",
    Attention.L4: "需要处理",
    Attention.L5: "安全紧急",
}


#: Status → Attention 的默认推导。**只是默认值**：事件可以覆盖。
#: 刻意**延迟构建**：Attention 与 models 互相需要（快照存 Attention，
#: 默认表以 Status 为键），运行期才取 models，避免循环导入。
_DEFAULT_MAP: "dict | None" = None


def _default_map() -> "dict":
    global _DEFAULT_MAP
    if _DEFAULT_MAP is None:
        from .models import Status

        _DEFAULT_MAP = {
            Status.IDLE: Attention.L0,
            Status.STARTING: Attention.L0,
            Status.RUNNING: Attention.L0,
            Status.RETRYING: Attention.L1,
            Status.DONE: Attention.L1,
            Status.CANCELLED: Attention.L2,
            Status.PAUSED: Attention.L2,
            Status.WAITING_INPUT: Attention.L3,
            Status.WAITING_APPROVAL: Attention.L3,
            Status.BLOCKED: Attention.L4,
            Status.ERROR: Attention.L4,
            Status.WARNING: Attention.L4,
            Status.UNKNOWN: Attention.L1,
        }
    return _DEFAULT_MAP


def attention_for_status(status: object) -> Attention:
    """默认推导。永不抛异常。"""
    try:
        return _default_map().get(status, Attention.L1)
    except Exception:  # noqa: BLE001 - 推导失败也不能崩
        return Attention.L1


def parse_attention(raw: object) -> Attention | None:
    """把 ``"L5" / 5 / "5" / "l4"`` 解析成 Attention；解析不了返回 None。

    None 表示「这条事件没有表达 attention」，不是 L0——
    缺省和显式归零是两回事。
    """
    if raw is None:
        return None
    if isinstance(raw, Attention):
        return raw
    if isinstance(raw, bool):
        return None
    try:
        n = int(raw)  # 5 / "5"
    except (TypeError, ValueError):
        text = str(raw).strip().lower()
        if text.startswith("l") and text[1:].isdigit():
            n = int(text[1:])
        else:
            return None
    if 0 <= n <= 5:
        return Attention(n)
    return None


__all__ = [
    "Attention",
    "attention_for_status",
    "parse_attention",
]
