"""UAH v2 端到端集成测试 —— **真实 Executor / 真实 SessionController / 真实 Hub / 真实安全层**。

只有 skill 与 router 是测试桩（它们属于"业务内容"，不是本任务的对象），
**状态机、执行循环、控制通道、安全门、事件流全部是真的**：

    Executor.run(skill) ──写──▶ SessionController（真）
                                    │ on_change（真钩子）
                                    ▼
                        UhaNativeAdapter（真） ──▶ Hub（真 HTTP+SSE）──▶ 断言事件流
                                    ▲
    HUD 等价物: HubClient.submit_control ──▶ Hub /control ──▶ ControlPoller（真）
                                    │
                                    ▼
                             SessionController.pause/resume/stop（真）
                                    ▼
                    Executor 的下一次 should_abort/wait_if_paused/ensure_writable 真实生效

覆盖的验收项：#8 Pause 真的暂停、#9 Resume 真的恢复、#10 Stop 真的终止、
#17 Action 成功但 Verification 失败时**不允许**显示完成、#18 验证通过才进完成、
#20 最近活动与真实执行历史一致、#12/#14 安全层不依赖 HUD。

用法：``python uah/tests/v2_integration.py``（任意解释器，不需要 tkinter）
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Mapping

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.core.config import load_config  # noqa: E402
from src.scheduler.executor import Executor  # noqa: E402
from src.skills.base import Skill, SkillContext, SkillTrace  # noqa: E402
from src.validation.result import CheckResult, VerifyReport  # noqa: E402
from uah.adapters.uha.adapter import UhaNativeAdapter, attach_plan_progress  # noqa: E402
from uah.adapters.publisher import LocalPublisher  # noqa: E402
from uah.core.control_poller import ControlPoller  # noqa: E402
from uah.core.transport import HubServer, pick_free_port  # noqa: E402
from uah.safety.bridge import SAFETY_AGENT_ID, SafetyBridge  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"   [{detail}]" if detail else ""), flush=True)


# ---------------------------------------------------------------------------
# 测试桩：skill / router / bundle（**只有这些是桩**）
# ---------------------------------------------------------------------------


class StubSkill(Skill):
    """最小可执行 skill：perform 受测试控制，verify 结论受测试控制。"""

    name = "integration_probe"
    description = "UAH v2 集成测试用 skill"
    supported_methods = frozenset({"UE_PYTHON"})
    preferred_methods = ("UE_PYTHON",)

    def __init__(self) -> None:
        self.perform_calls: list[float] = []
        self.verify_calls = 0
        self.perform_seconds = 0.0
        self.verify_passes = True
        self.perform_error: Exception | None = None

    def intent(self, params: Mapping[str, Any], **overrides: Any):
        from src.router.intent import TaskIntent

        kw: dict[str, Any] = {
            "skill": self.name,
            "description": "集成测试：写入类任务（走 ApprovalGate + Router）",
            "target_count": 1,
            "needs_exact_values": True,
            "known_actor": str(params.get("actor") or "Test"),
            "read_only": False,
            "needs_vision": False,
        }
        kw.update(overrides)
        return TaskIntent(**kw)

    def task_category(self, params: Mapping[str, Any] | None = None) -> str:
        return "general"

    def is_idempotent(self, params: Mapping[str, Any]) -> bool:
        return True

    def preflight(self, ctx: SkillContext, params: Mapping[str, Any]) -> dict[str, Any]:
        return {"before": {}}

    def perform(self, ctx: SkillContext, method: str, params: Mapping[str, Any],
                trace: SkillTrace) -> dict[str, Any]:
        self.perform_calls.append(time.time())
        # 真的睡：给"暂停/停止"留出真实的时间窗口
        deadline = time.time() + self.perform_seconds
        while time.time() < deadline:
            time.sleep(0.02)
        if self.perform_error is not None:
            raise self.perform_error
        return {"ok": True}

    def verify(self, ctx: SkillContext, method: str, params: Mapping[str, Any],
               trace: SkillTrace) -> VerifyReport:
        self.verify_calls += 1
        report = VerifyReport()
        if self.verify_passes:
            report.add(CheckResult(name="integration", passed=True, detail="桩校验通过"))
        else:
            # "API 调用成功，但场景没生效" —— 这正是要防的假绿形态
            report.add(CheckResult(name="integration", passed=False,
                                   expected="position changed", actual="unchanged",
                                   detail="桩校验失败：写入成功但状态未变化"))
        return report


class StubDecision:
    def __init__(self, method: str = "UE_PYTHON") -> None:
        self.method = method
        self.fallback_order: list[str] = []
        self.reason = "集成测试桩：直接指定 UE_PYTHON"
        self.rule = "stub"
        self.score = 1.0
        self.alternatives: list[str] = []


class StubRouter:
    def __init__(self) -> None:
        self.feedback_calls = 0

    def decide(self, *a: Any, **kw: Any) -> StubDecision:
        return StubDecision()

    def feedback(self, *a: Any, **kw: Any) -> None:
        self.feedback_calls += 1


class StubBundle:
    """只提供 Executor.run() 路由阶段需要的最小接口。"""

    def structured_backends(self) -> dict[str, Any]:
        return {}

    def __getattr__(self, item: str) -> Any:  # 其它属性一律不存在
        raise AttributeError(item)


class StubLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def event(self, kind: str, **fields: Any) -> None:
        self.events.append((kind, fields))

    def error(self, *a: Any, **kw: Any) -> None:
        self.events.append(("error", {"args": a, "kw": kw}))

    def warn(self, *a: Any, **kw: Any) -> None:
        pass

    def info(self, *a: Any, **kw: Any) -> None:
        pass


def build_executor(cfg: Any, logger: Any) -> Executor:
    ex = Executor(bundle=StubBundle(), config=cfg, logger=logger)  # type: ignore[arg-type]
    ex.router = StubRouter()  # type: ignore[assignment]
    # 绕开真实探测（本机没有 UE / MCP 在线），只保留"UE_PYTHON 可用"这一事实
    ex.probe = lambda **kw: {"UE_PYTHON": True, "UNREAL_MCP": False, "MOUSE": False,
                             "VISION": False, "HYBRID": False}  # type: ignore[assignment]
    ex.approval.granted_by_default = "integration_test"  # type: ignore[attr-defined]
    return ex


def wait_for(predicate, timeout: float = 5.0, step: float = 0.05) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(step)
    return False


def timeline_types(server: HubServer, agent: str = "uha") -> list[str]:
    return [str(r.get("type")) for r in server.timeline(agent, limit=100)]


def new_types(server: HubServer, since: float, agent: str = "uha") -> list[str]:
    """只看 ``since`` 之后的事件（时间线是持久账本，跨用例必须按时间切片）。"""
    return [str(r.get("type")) for r in server.timeline(agent, limit=200)
            if float(r.get("ts") or 0) >= since]


def main() -> int:
    print("=== UAH v2 端到端集成测试（真实 Executor / Controller / Hub / Safety）===", flush=True)

    cfg = load_config(None, reload=True)
    cfg.data.setdefault("execution", {})["mode"] = "AUTO"

    # --- 真实 SessionController 单例（Executor 与适配器共用同一个） ----------
    from src.core.session_control import reset_controller, get_controller

    reset_controller()
    controller = get_controller()

    from src.core.log import RunLogger

    # 用**真实 RunLogger**（Executor 会用它开 step 上下文），日志落到临时目录
    log_dir = Path(tempfile.mkdtemp(prefix="uah-v2-integration-")) / "logs"
    logger = RunLogger(log_dir, run_name="uah-v2-integration", console=False)
    executor = build_executor(cfg, logger)

    port = pick_free_port()
    server = HubServer(port=port).start()
    url = server.url()
    adapter = UhaNativeAdapter(LocalPublisher(server), agent_id="uha", agent_name="UHA",
                               phase_label="Integration", heartbeat_s=0.0)
    adapter.start()
    adapter.attach_controller(controller, emit_now=True)
    hook_info = attach_plan_progress(adapter, executor)
    poller = ControlPoller(url, controller, wait_s=0.5)
    poller.start()
    token = server.approval_token

    check("Executor 的进度钩子已接到真实 Executor 上",
          hook_info.get("progress_hook") is True, str(hook_info))

    try:
        # ==================================================================
        # 1) 正常成功路径：perform 成功 + verify 通过 → DONE
        # ==================================================================
        skill = StubSkill()
        result = executor.run(skill, {"actor": "Test", "axis": "z", "delta": 10})
        adapter.flush(2.0)
        check("真实 Executor 运行成功（verify 通过）", result.ok is True,
              f"ok={result.ok} err={result.error}")
        snap = server.store.snapshot("uha")
        check("UAH 收到 DONE", snap is not None and snap.status.value == "DONE",
              snap.status.value if snap else "no snapshot")
        types = timeline_types(server)
        check("时间线含 verification.started（验证真的开始过）", "verification.started" in types,
              str(types[:8]))
        check("时间线含 verification.passed", "verification.passed" in types, str(types[:8]))
        check("时间线含 task.completed（真实执行历史）", "task.completed" in types, str(types[:8]))
        check("执行方式作为 Tool 显示", (snap.activity.tool or "") == "UE Python",
              str(snap.activity.tool))
        check("DONE 的 Attention 为 L1（一般状态变化，不打扰）",
              int(snap.attention.value) == 1, str(snap.attention))

        # ==================================================================
        # 2) 假绿防护：perform 成功 + verify 失败 → **绝不能显示完成**
        # ==================================================================
        server.store.clear()
        t_fail = time.time()
        skill2 = StubSkill()
        skill2.verify_passes = False
        result2 = executor.run(skill2, {"actor": "Test", "axis": "z", "delta": 10})
        adapter.flush(2.0)
        snap2 = server.store.snapshot("uha")
        types2 = new_types(server, t_fail)
        check("verify 失败时 Executor 判失败", result2.ok is False, f"ok={result2.ok}")
        check("verify 失败 → UAH **不显示 DONE**",
              snap2 is not None and snap2.status.value != "DONE",
              snap2.status.value if snap2 else "no snapshot")
        check("时间线含 verification.failed", "verification.failed" in types2, str(types2[:8]))
        check("失败后 Attention 升到 L4（必须人工处理）",
              snap2 is not None and int(snap2.attention.value) >= 4,
              str(snap2.attention) if snap2 else "none")
        check("action 成功但 verify 失败时**没有** completed 事件",
              "task.completed" not in types2, str(types2[:8]))

        # ==================================================================
        # 3) 暂停：HUD 等价路径（Hub /control）→ 真的阻断执行
        # ==================================================================
        t_pause = time.time()
        resp = _hud_control(server, url, token, "pause")
        check("HUD 控制命令被 Hub 接受", resp.get("ok") is True, str(resp))
        applied = wait_for(lambda: controller.snapshot().command.value == "PAUSE", 3.0)
        check("暂停命令真的传到 SessionController", applied, str(controller.snapshot().command))
        check("暂停后写操作被真实阻断（executor 的写前门）",
              controller.blocks_writes() is True)
        check("暂停态相位为 PAUSED", controller.snapshot().phase.value == "PAUSED",
              controller.snapshot().phase.value)
        # 暂停期间**绝不执行写操作**：真实 Executor 的 ensure/blocks_writes 检查
        # （executor.py 内对 write/gui 通道的硬拦）会直接拒绝
        from src.core.errors import PreconditionFailed

        write_blocked = False
        write_error = ""
        try:
            controller.ensure_writable("integration:probe")
        except PreconditionFailed as exc:
            write_blocked = True
            write_error = str(exc)
        except Exception as exc:  # noqa: BLE001
            write_error = f"{type(exc).__name__}: {exc}"
        check("暂停期间的写操作被硬拦（executor 写前门抛 PreconditionFailed）",
              write_blocked, write_error)

        adapter.flush(1.5)
        check("UAH 侧收到 task.paused（真实暂停事件）",
              "task.paused" in new_types(server, t_pause), str(new_types(server, t_pause)[:6]))
        snap_p = server.store.snapshot("uha")
        check("UAH 侧状态显示已暂停",
              snap_p is not None and snap_p.status.value == "PAUSED",
              snap_p.status.value if snap_p else "none")
        check("PAUSED 的 Attention 为 L2", snap_p is not None and int(snap_p.attention.value) == 2,
              str(snap_p.attention) if snap_p else "none")

        # 暂停期间发起的运行会被真实拦下（走 executor 自己的检查，不是测试桩）
        t_blocked = time.time()
        skill_p = StubSkill()
        skill_p.perform_seconds = 0.0
        blocked_result = executor.run(skill_p, {"actor": "Test", "axis": "z", "delta": 10})
        adapter.flush(1.0)
        check("暂停期间发起的运行真的被拦下（不是静默放行）",
              blocked_result.ok is False
              and ("暂停" in str(blocked_result.error or "")
                   or "控制" in str(blocked_result.error or "")),
              f"ok={blocked_result.ok} err={blocked_result.error}")
        check("被拦下的运行没有产生 task.completed",
              "task.completed" not in new_types(server, t_blocked),
              str(new_types(server, t_blocked)[:6]))

        # ==================================================================
        # 4) 恢复：Resume 真的让执行回到可用状态
        # ==================================================================
        t_resume = time.time()
        _hud_control(server, url, token, "resume")
        resumed = wait_for(lambda: controller.snapshot().command.value != "PAUSE", 3.0)
        check("Resume 真的解除了暂停", resumed and not controller.blocks_writes(),
              f"command={controller.snapshot().command} blocks={controller.blocks_writes()}")
        skill4 = StubSkill()
        skill4.perform_seconds = 0.0
        result4 = executor.run(skill4, {"actor": "Test", "axis": "z", "delta": 10})
        adapter.flush(1.5)
        check("恢复后运行真的能跑通", result4.ok is True, f"ok={result4.ok} err={result4.error}")
        check("时间线含 task.resumed（真实恢复事件）",
              "task.resumed" in new_types(server, t_resume), str(new_types(server, t_resume)[:8]))

        # ==================================================================
        # 5) 停止：真的终止任务（下一次运行会被 executor 立刻中止）
        # ==================================================================
        t_stop = time.time()
        _hud_control(server, url, token, "stop")
        aborted = wait_for(lambda: controller.should_abort(), 3.0)
        check("停止请求被真实受理（should_abort=True）", aborted,
              f"command={controller.snapshot().command}")
        skill5 = StubSkill()
        skill5.perform_seconds = 0.0
        result5 = executor.run(skill5, {"actor": "Test", "axis": "z", "delta": 10})
        adapter.flush(1.0)
        check("停止后运行被真实中止（executor 返回中止）",
              result5.ok is False and "中止" in str(result5.error or ""),
              f"ok={result5.ok} err={result5.error}")
        check("停止后**没有**任何 perform 被执行", skill5.perform_calls == [],
              f"perform_calls={len(skill5.perform_calls)}")

        # 复位（用显式恢复，避免把 STOP 留在会话里影响后续用例）
        controller.acknowledge_control()
        wait_for(lambda: not controller.should_abort(), 3.0)

        # 急停不通过 HUD 控制通道（防止以后有人"顺手加上"）
        estop_resp = _hud_control(server, url, token, "emergency_stop")
        check("HUD 控制通道**拒绝** emergency_stop（急停只走热键）",
              estop_resp.get("ok") is False, str(estop_resp))

        # ==================================================================
        # 6) 安全层：真实 SafetyController → L5 事件 + 热键；且不依赖 HUD/桥
        # ==================================================================
        from src.safety.controller import SafetyController

        with tempfile.TemporaryDirectory() as tmp:
            gate_path = Path(tmp) / "safety_gate.json"
            safety = SafetyController(state_path=gate_path, hotkey="ctrl+alt+f12")
            bridge = SafetyBridge(LocalPublisher(server))
            attached = bridge.attach(safety)
            check("安全桥接入真实 SafetyController", attached is True)

            safety.set_banner_available(True)
            safety.request_control(task="集成测试")
            safety.banner_visible(ok=True)
            safety.grant_control()
            bridge.flush(2.0)
            snap_s = server.store.snapshot(SAFETY_AGENT_ID)
            check("输入控制被接管 → UAH 收到安全条目",
                  snap_s is not None and snap_s.status.value == "RUNNING",
                  snap_s.status.value if snap_s else "none")
            check("输入控制接管 → Attention L5",
                  snap_s is not None and int(snap_s.attention.value) == 5,
                  str(snap_s.attention) if snap_s else "none")
            check("安全条目里带真实急停热键",
                  snap_s is not None and "hotkey=ctrl+alt+f12"
                  in (snap_s.activity.detail or ""),
                  (snap_s.activity.detail if snap_s else "none"))
            check("时间线含 input.control_acquired",
                  "input.control_acquired" in timeline_types(server, SAFETY_AGENT_ID),
                  str(timeline_types(server, SAFETY_AGENT_ID)[:5]))

            # ★ HUD 崩溃：桥停掉之后，安全层必须**照常有效**（安全不依赖 HUD）
            bridge.stop()
            safety.emergency_stop(reason="integration_test")
            bridge2 = SafetyBridge(LocalPublisher(server))
            bridge2.attach(safety)
            bridge2.flush(2.0)
            snap_e = server.store.snapshot(SAFETY_AGENT_ID)
            check("桥停掉后急停仍然生效（安全不依赖 HUD）",
                  safety.state.value == "EMERGENCY_STOP", safety.state.value)
            check("急停后 UAH 侧为 L5 + ERROR",
                  snap_e is not None and int(snap_e.attention.value) == 5
                  and snap_e.status.value == "ERROR",
                  f"{snap_e.status.value if snap_e else 'none'}/"
                  f"{snap_e.attention if snap_e else 'none'}")
            check("急停事件进了时间线",
                  "safety.emergency_stop" in timeline_types(server, SAFETY_AGENT_ID),
                  str(timeline_types(server, SAFETY_AGENT_ID)[:6]))
            safety.resume_safety(explicit=True)
            bridge2.stop()

        # ==================================================================
        # 7) 执行方式切换（Computer Use / Script / MCP）→ UAH 侧自动更新
        # ==================================================================
        # 用**真实的** SessionController API 切换（executor 就是这么写的），
        # 再断言 UAH 侧看到的 Tool 文案；不是往 Hub 里灌假事件。
        method_cases = [
            ("MOUSE", "COMPUTER_CONTROL", "Mouse", "Computer Use 接管键鼠"),
            ("UE_PYTHON", "STRUCTURED_EXECUTION", "UE Python", "Script 通道"),
            ("UE_COMMANDLET", "STRUCTURED_EXECUTION", "UE Commandlet", "Commandlet Script"),
            ("UNREAL_MCP", "STRUCTURED_EXECUTION", "Unreal MCP", "MCP 通道"),
        ]
        from src.core.session_control import TaskPhase

        for method, phase, expect_tool, label in method_cases:
            controller.begin_task(f"method:{method}")
            controller.set_step(f"method:{method}", status="执行中")
            controller.set_method(method)
            controller.set_phase(TaskPhase[phase], controls_mouse=(phase == "COMPUTER_CONTROL"))
            adapter.flush(1.0)
            snap_m = server.store.snapshot("uha")
            got = snap_m.activity.tool if snap_m else None
            check(f"{label} → UI 显示正确（{method} → {expect_tool}）", got == expect_tool, str(got))
            if phase == "COMPUTER_CONTROL":
                check("Computer Use 时 phase 映射为 RUNNING（不泄露内部阶段）",
                      snap_m is not None and snap_m.status.value == "RUNNING",
                      snap_m.status.value if snap_m else "none")
        controller.end_task(ok=True)
        adapter.flush(0.5)

        # ==================================================================
        # 8) 计划进度（Step i/N）：真实 attach_plan_progress 钩子
        # ==================================================================
        server.store.clear()
        adapter.plan_begin("集成计划", 3)
        adapter.plan_step(1, "integration_probe")
        adapter.flush(1.0)
        snap_p = server.store.snapshot("uha")
        check("计划进度落到 Step 1/3",
              snap_p is not None and snap_p.task.step == 1 and snap_p.task.total_steps == 3,
              f"{snap_p.task.step}/{snap_p.task.total_steps}" if snap_p else "none")
        adapter.plan_end(ok=True)
        adapter.flush(1.0)
    finally:
        poller.stop()
        adapter.stop(reason="integration test end")
        adapter.flush(1.0)
        server.stop()

    failed = [c for c in CHECKS if not c[1]]
    print(f"\n{'全部通过' if not failed else '失败'}（{len(CHECKS) - len(failed)}/{len(CHECKS)}）")
    for name, _ok, detail in failed:
        print(f"  - {name}  {detail}")
    return 1 if failed else 0


def _hud_control(server: HubServer, url: str, token: str, action: str) -> dict:
    """站在 HUD 的位置提交软控制命令（走真实 HTTP + 能力令牌）。"""
    from uah.core.transport import HubClient

    return HubClient(url, timeout_s=2.0).submit_control(action, token=token)


if __name__ == "__main__":
    raise SystemExit(main())
