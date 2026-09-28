"""Allowlisted runtime configuration, never credentials or arbitrary config."""
from pathlib import Path


def runtime_settings(cfg, executor):
    project = cfg.get("ue.project_file")
    gate = getattr(executor, "approval", None)
    hotkey = getattr(executor, "_hotkey", None)
    hotkey_active = bool(hotkey and (getattr(hotkey, "registered", False) or
                         getattr(hotkey, "_use_polling", False)))
    controller=getattr(executor,'controller',None)
    command=controller.snapshot().command if controller else None
    return {
        "task_domain": Path(project).stem if project else "未配置 UE 项目",
        "dry_run": bool(cfg.is_dry_run()),
        "approval": "已接入审批策略" if gate is not None else "当前运行时未接入审批策略",
        "never_delete_assets": bool(cfg.get("safety.never_delete_assets", True)),
        "never_modify_core_config": bool(cfg.get("safety.never_modify_core_config", True)),
        "max_actors_per_batch": cfg.get("safety.max_actors_per_batch", 500),
        "hotkey": str(cfg.get("desktop.emergency_hotkey") or "ctrl+alt+f12"),
        "hotkey_active": hotkey_active,
        "soft_control": True,
        "control_command": str(getattr(command,'value',command) or 'NONE'),
        "settings_access": "read_only",
    }
