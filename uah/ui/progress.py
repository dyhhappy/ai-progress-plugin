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
    if snap.runtime.get('display_mode') == 'activity':
        if not connected or snap.stale or snap.stopped:
            return ProgressView('hidden', None, '已离线 · 工作过程暂不可用', 'muted')
        if snap.runtime.get('feedback_error'):
            return ProgressView('hidden', None, '过程反馈失败 · 请检查钩子', 'danger')
        if not snap.runtime.get('feedback_connected'):
            if snap.runtime.get('source')=='desktop-app':
                return ProgressView('hidden',None,'等待桌面反馈 · 请检查工作目录与钩子','wait')
            return ProgressView('hidden', None, '等待过程反馈 · 请在 /hooks 检查钩子', 'wait')
        text=snap.activity.summary or '等待工作阶段更新'
        active=snap.status in (Status.RUNNING,Status.STARTING,Status.RETRYING)
        tone='ok' if snap.status is Status.DONE else 'danger' if snap.status is Status.ERROR else 'active' if active else 'wait'
        return ProgressView('indeterminate' if active else 'hidden',None,text,tone)
    ratio = task.progress_ratio
    basis = "已上报" if snap.runtime.get("progress_basis") == "reported" else "已验证"
    detail = f"{basis} {task.completed_steps}/{task.total_steps}" if ratio is not None else (
        f"当前步骤 {task.progress_text}" if task.progress_text else "总量未知")
    if not connected or snap.stale or snap.stopped:
        return ProgressView("determinate" if ratio is not None else "hidden", ratio,
                            f"离线 · {detail}", "muted")
    status = snap.status
    if snap.runtime.get('feedback_error'):
        return ProgressView('hidden', None, str(snap.runtime['feedback_error'])+' · 进度暂不可用', 'danger')
    estimate = snap.runtime.get('estimated_percent')
    if ratio is None and isinstance(estimate,(int,float)) and not isinstance(estimate,bool) and 0 <= estimate <= 99:
        ratio = estimate / 100
        detail = 'AI 估计'
    if status is Status.DONE and snap.runtime.get('source') == 'cli-wrapper' and snap.runtime.get('feedback_connected'):
        suffix = ' · 等待下一个问题' if snap.runtime.get('process_running') else ''
        return ProgressView('determinate', 1.0, '本轮回答已结束 · 100%' + suffix, 'ok')
    if ratio is None and snap.runtime.get('source') == 'cli-wrapper':
        if snap.runtime.get('feedback') == 'codex' and not snap.runtime.get('feedback_connected'):
            detail = '反馈未连接 · 请在 Codex /hooks 检查钩子'
        elif snap.runtime.get('feedback_connected') and status in (Status.RUNNING,Status.STARTING):
            ratio = 0.0
            detail = '本轮已开始 · 等待阶段上报'
    if status is Status.IDLE:
        return ProgressView()
    tone = ("ok" if status is Status.DONE else "danger" if status is Status.ERROR
            else "wait" if status in (Status.PAUSED, Status.WAITING_INPUT,
                Status.WAITING_APPROVAL, Status.BLOCKED, Status.WARNING)
            else "muted" if status is Status.CANCELLED else "active")
    # Even a malformed producer must not display 100% while still executing.
    if ratio is not None and ratio >= 1 and status is not Status.DONE:
        ratio = .99 if snap.runtime.get('source') == 'cli-wrapper' else None
        detail = f"步骤{basis}，等待任务结果" if tone == "active" else detail
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
