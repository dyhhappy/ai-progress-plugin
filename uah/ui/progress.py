"""Pure presentation rules shared by the desktop views and headless checks."""
from dataclasses import dataclass
import time
from ..core.models import Status


@dataclass(frozen=True)
class ProgressView:
    mode: str = "hidden"
    ratio: float | None = None
    text: str = ""
    tone: str = "idle"


def progress_view(snap, connected=True):
    if snap is None:
        return ProgressView()
    task = snap.task
    ratio = task.progress_ratio
    detail = f"已验证 {task.completed_steps}/{task.total_steps}" if ratio is not None else (
        f"当前步骤 {task.progress_text}" if task.progress_text else "总量未知")
    if not connected or snap.stale or snap.stopped:
        return ProgressView("determinate" if ratio is not None else "hidden", ratio,
                            f"离线 · {detail}", "muted")
    status = snap.status
    if status is Status.IDLE:
        return ProgressView()
    tone = ("ok" if status is Status.DONE else "danger" if status is Status.ERROR
            else "wait" if status in (Status.PAUSED, Status.WAITING_INPUT,
                Status.WAITING_APPROVAL, Status.BLOCKED, Status.WARNING)
            else "muted" if status is Status.CANCELLED else "active")
    # Even a malformed producer must not display 100% while still executing.
    if ratio is not None and ratio >= 1 and status is not Status.DONE:
        ratio = None
        detail = "步骤已验证，等待任务结果" if tone == "active" else detail
    prefix = {Status.DONE: "已完成", Status.ERROR: "失败", Status.CANCELLED: "已停止",
              Status.PAUSED: "暂停中", Status.WARNING: "需要检查"}.get(status, "")
    active = status in (Status.RUNNING, Status.STARTING, Status.RETRYING)
    mode = "determinate" if ratio is not None else "indeterminate" if active else "hidden"
    if active and snap.progress_updated_at and time.time() - snap.progress_updated_at >= 60:
        detail += f" · {int(time.time()-snap.progress_updated_at)}秒无进度更新（仍在线）"
    percent = f" · {int(ratio * 100)}%" if ratio is not None else ""
    return ProgressView(mode, ratio, " · ".join(x for x in (prefix, detail) if x) + percent, tone)


def task_elapsed(snap, now=None):
    start = snap.task.started_at
    if start is None:
        return "—"
    end = snap.task.ended_at
    if end is None:
        end = snap.updated_at if snap.stale or snap.stopped else (time.time() if now is None else now)
    total = int(max(0, end - start))
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"
