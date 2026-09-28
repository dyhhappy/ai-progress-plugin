"""Headless regression suite. Uses real Executor/Hub/SSE; no input automation."""
import copy
import os
import sys
import time
import tempfile
import threading
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from uah.core.models import TaskState, AgentSnapshot, AgentRef, Status
from uah.core.state import StateStore
from uah.core.transport import HubServer, HubClient
from uah.core.control_poller import ControlPoller
from uah.core.approval import fetch_control_commands, acknowledge_control
from uah.adapters.publisher import LocalPublisher
from uah.adapters.uha.adapter import UhaNativeAdapter, attach_plan_progress
from uah.ui.progress import progress_view, task_elapsed
from uah.ui.monitors import clamp_position, tk_position, work_areas
from uah.ui.runtime_settings import runtime_settings
from src.core.session_control import SessionController, TaskPhase
from src.core.config import Config
from src.core.log import RunLogger
from src.scheduler.executor import Executor
from src.runtime import BackendBundle
from src.router.router import Decision
from src.router.intent import TaskIntent
from src.skills.base import Skill
from src.validation.result import VerifyReport, CheckResult
from src.planner.task import Plan, PlanStep


def wait_for(fn, timeout=4):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(.02)
    raise AssertionError('Timed out waiting for condition')


class ModelTests(unittest.TestCase):
    def test_flush_waits_for_in_flight_publication(self):
        entered=threading.Event();release=threading.Event()
        class SlowPublisher:
            def publish(self,payload):
                entered.set();release.wait(timeout=2)
        adapter=UhaNativeAdapter(SlowPublisher(),heartbeat_s=0)
        adapter.start()
        try:
            self.assertTrue(entered.wait(timeout=1))
            self.assertFalse(adapter.flush(.02))
            release.set()
            self.assertTrue(adapter.flush(1))
        finally:
            release.set();adapter.stop()

    def test_new_task_clears_old_progress(self):
        s = StateStore()
        s.apply({'agent': {'id':'a'}, 'type':'task.started',
                 'task':{'id':'old','step':3,'total_steps':3,'completed_steps':3}})
        s.apply({'agent': {'id':'a'}, 'type':'task.started', 'task':{'id':'new','name':'single'}})
        self.assertIsNone(s.snapshot('a').task.step)
        self.assertIsNone(s.snapshot('a').task.progress_ratio)

    def test_explicit_null_and_partial_update(self):
        t = TaskState(step=3, total_steps=3, completed_steps=2)
        self.assertEqual(t.merged(TaskState(stage='verify')).step, 3)
        cleared = t.merged(TaskState.from_wire({'step':None,'total_steps':None}))
        self.assertIsNone(cleared.step)
        self.assertIsNone(cleared.progress_ratio)

    def test_current_step_does_not_mean_completed(self):
        self.assertIsNone(TaskState(step=3,total_steps=3).progress_ratio)
        self.assertEqual(TaskState(step=3,total_steps=3,completed_steps=2).progress_ratio, 2/3)
        self.assertIsNone(TaskState(total_steps=0,completed_steps=0).progress_ratio)

    def test_status_matrix_and_no_premature_hundred(self):
        for status in Status:
            snap = AgentSnapshot(AgentRef(id='a'),status=status,
                task=TaskState(step=3,total_steps=3,completed_steps=3))
            view = progress_view(snap)
            if status is not Status.DONE:
                self.assertNotEqual(view.ratio,1)
        snap.status = Status.DONE
        self.assertEqual(progress_view(snap).ratio,1)

    def test_offline_stale_paused_terminal_stop_animation(self):
        s = AgentSnapshot(AgentRef(id='a'),status=Status.RUNNING)
        self.assertEqual(progress_view(s).mode,'indeterminate')
        self.assertNotEqual(progress_view(s,False).mode,'indeterminate')
        s.stale=True
        self.assertNotEqual(progress_view(s).mode,'indeterminate')
        s.stale=False
        for status in (Status.PAUSED,Status.ERROR,Status.CANCELLED,Status.DONE,Status.WARNING):
            s.status=status
            self.assertNotEqual(progress_view(s).mode,'indeterminate')

    def test_elapsed_is_task_scoped_and_frozen(self):
        s=AgentSnapshot(AgentRef(id='a'), first_seen_at=1,
            task=TaskState(started_at=100,ended_at=160))
        self.assertEqual(task_elapsed(s,500),'01:00')
        s.task=TaskState(started_at=200)
        self.assertEqual(task_elapsed(s,205),'00:05')
        s.stale=True; s.updated_at=220
        self.assertEqual(task_elapsed(s,500),'00:20')

    def test_negative_monitors_and_unplug(self):
        areas=[(-1920,0,0,1040),(0,0,1920,1040),(0,-1080,1920,0)]
        self.assertEqual(clamp_position(-1800,100,452,150,areas),(-1800,100))
        self.assertEqual(clamp_position(100,-1000,452,150,areas),(100,-1000))
        self.assertEqual(clamp_position(-1800,100,452,150,[areas[1]]),(0,100))
        self.assertEqual(clamp_position(1800,1000,452,150,areas),(1468,890))
        self.assertEqual(tk_position(-1800,-200),'+-1800+-200')
        self.assertTrue(work_areas())

    def test_settings_whitelist(self):
        cfg=Config({'ue':{'project_file':'E:/demo/Game.uproject'},
                    'secret':'password', 'safety':{'never_delete_assets':True}})
        meta=runtime_settings(cfg,NS())
        self.assertEqual(meta['task_domain'],'Game')
        self.assertEqual(meta['settings_access'],'read_only')
        self.assertNotIn('secret',meta)
        self.assertFalse(meta['hotkey_active'])

    def test_no_progress_is_not_fabricated_failure(self):
        snap=AgentSnapshot(AgentRef(id='a'),status=Status.RUNNING,
            progress_updated_at=time.time()-90)
        self.assertIn('无进度更新',progress_view(snap).text)
        self.assertIn('仍在线',progress_view(snap).text)
        self.assertEqual(snap.status,Status.RUNNING)

    def test_generic_task_start_resets_clock_and_unknown_total(self):
        s=StateStore()
        s.apply({'agent':{'id':'a'},'type':'task.started','status':'RUNNING',
                 'task':{'name':'a','step':3,'total_steps':3,'completed_steps':2}})
        first=s.snapshot('a').task.started_at
        s.apply({'agent':{'id':'a'},'type':'task.started','status':'RUNNING',
                 'task':{'name':'b'}})
        self.assertGreaterEqual(s.snapshot('a').task.started_at,first)
        self.assertIsNone(s.snapshot('a').task.progress_ratio)

    def test_malformed_progress_is_bounded(self):
        for raw in ({'completed_steps':-9,'total_steps':3},
                    {'completed_steps':99,'total_steps':3},
                    {'completed_steps':'bad','total_steps':3},
                    {'completed_steps':1,'total_steps':-1}):
            ratio=TaskState.from_wire(raw).progress_ratio
            self.assertTrue(ratio is None or 0<=ratio<=1)

    def test_sse_signature_catches_same_clock_tick_progress(self):
        s=StateStore(clock=lambda:100.0)
        s.apply({'agent':{'id':'a'},'seq':1,'type':'task.step_changed',
                 'status':'RUNNING','task':{'step':1}})
        before=s.signature_of(s.snapshot('a').to_wire())
        s.apply({'agent':{'id':'a'},'seq':2,'type':'task.step_changed',
                 'status':'RUNNING','task':{'step':2}})
        self.assertNotEqual(before,s.signature_of(s.snapshot('a').to_wire()))


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.hub=HubServer(port=0,store=StateStore(stale_after_s=.35)).start()
        self.url=self.hub.url()
    def tearDown(self):
        self.hub.stop()

    def test_targeted_control_and_replay(self):
        cmd=self.hub.submit_control('pause',agent_id='other')
        self.assertEqual(fetch_control_commands(self.url,agent_id='uha'),[])
        jobs=fetch_control_commands(self.url,agent_id='other')
        self.assertEqual(len(jobs),1)
        self.assertTrue(acknowledge_control(self.url,jobs[0],True,'applied')['ok'])
        self.assertFalse(acknowledge_control(self.url,jobs[0],True,'replay')['ok'])
        self.assertEqual(self.hub.control_result(cmd['id'])['state'],'applied')
        self.assertFalse(self.hub.submit_control('emergency_stop')['ok'])

    def test_actual_controller_acknowledgement(self):
        ctrl=SessionController(); ctrl.begin_task('job')
        poller=ControlPoller(self.url,ctrl,wait_s=.05)
        poller.start()
        try:
            for action,phase in [('pause',TaskPhase.PAUSED),('resume',TaskPhase.ANALYZING),('stop',TaskPhase.STOPPING)]:
                res=HubClient(self.url).submit_control(action,token=self.hub.approval_token)
                wait_for(lambda:self.hub.control_result(res['id']).get('state')=='applied')
                self.assertEqual(ctrl.snapshot().phase,phase)
        finally:
            poller.stop()

    def test_expired_control_never_executes(self):
        res=self.hub.submit_control('stop')
        self.hub._pending_control[0]['expires_at']=time.time()-1
        self.hub._control_results[res['id']]['expires_at']=time.time()-1
        self.assertEqual(self.hub.take_control_commands(),[])
        self.assertEqual(self.hub.control_result(res['id'])['state'],'expired')

    def test_control_cannot_clear_stop_and_reports_exception(self):
        ctrl=SessionController();ctrl.stop()
        poller=ControlPoller(self.url,ctrl)
        for action in ('pause','resume'):
            self.assertFalse(poller._apply({'action':action}))
            self.assertTrue(ctrl.should_abort())
        ctrl.acknowledge_control();ctrl.begin_task('a')
        with patch.object(ctrl,'pause',side_effect=RuntimeError('cannot pause')):
            self.assertFalse(poller._apply({'action':'pause'}))
            self.assertIn('cannot pause',poller.stats['last_error'])

    def test_control_requires_capability(self):
        result=HubClient(self.url).submit_control('pause',token='wrong')
        self.assertFalse(result['ok'])
        self.assertEqual(self.hub.take_control_commands(),[])

    def test_cli_setup_attaches_real_progress_and_control(self):
        import uha
        from uah.hosts.embedded.bootstrap import active_boot, uah_shutdown
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            cfg=Config({'logs':str(p/'logs'),
                'router':{'stats_file':str(p/'health.json'),'quality_file':str(p/'quality.json')},
                'workspace':{'artifact_dir':str(p/'artifacts')},
                'desktop':{'overlay':{'enabled':False}},
                'uah':{'port':self.hub.port,'notify':{'sound':False,'toast':False}}})
            with patch.object(uha,'load_config',return_value=cfg), \
                 patch.object(uha,'build_bundle',return_value=BackendBundle()), \
                 patch('uah.hosts.embedded.bootstrap._install_signal_note'):
                bundle,logger,executor=uha._setup(NS(config=None,dry_run=True,quiet=True))
            boot=active_boot()
            try:
                self.assertTrue(boot.info['plan_hooks']['progress_hook'])
                self.assertTrue(boot.info['control_poller']['poller'])
                boot.adapter.flush()
                snap=wait_for(lambda:self.hub.store.snapshot('uha'))
                self.assertIn('dry_run',snap.runtime,(boot.adapter.stats(),boot.embedded.delivered()))
                self.assertTrue(snap.runtime['dry_run'])
                self.assertTrue(snap.runtime['soft_control'])
            finally:
                uah_shutdown(boot)
                for h in logger._logger.handlers:h.close()

    def test_live_sse_reconnect_recovers_progress(self):
        from uah.adapters.publisher import HttpPublisher
        adapter=UhaNativeAdapter(HttpPublisher(self.url,timeout_s=.3),heartbeat_s=.08)
        stop=threading.Event();seen=[];connections=[]
        adapter.start();adapter.plan_begin('reconnect',4);adapter.plan_step(2,'probe')
        adapter.step_finished(1,True)
        reader=threading.Thread(target=lambda:HubClient(self.url).stream(
            lambda snap:seen.append(snap),on_status=connections.append,stop=stop,
            reconnect_delay_s=.05,read_timeout_s=2),daemon=True)
        reader.start()
        try:
            wait_for(lambda:seen and seen[-1].task.completed_steps==1)
            old_id=seen[-1].task.id
            port=self.hub.port
            self.hub.stop()
            self.hub=HubServer(port=port).start()
            wait_for(lambda:connections.count('connected')>=2)
            wait_for(lambda:self.hub.store.snapshot('uha') and
                     self.hub.store.snapshot('uha').task.id==old_id)
            wait_for(lambda:seen and seen[-1].task.id==old_id and seen[-1].task.completed_steps==1)
        finally:
            adapter.stop();stop.set();reader.join(timeout=3)
        self.assertFalse(reader.is_alive())

    def test_real_process_kill_detected_via_state_and_sse(self):
        code='''import sys,time
from uah.adapters.uha.adapter import UhaNativeAdapter
from uah.adapters.publisher import HttpPublisher
a=UhaNativeAdapter(HttpPublisher(sys.argv[1]),heartbeat_s=.06)
a.start();a.plan_begin("kill-probe",3);a.plan_step(1,"waiting");a.flush()
while True: time.sleep(.1)
'''
        child=subprocess.Popen([sys.executable,'-c',code,self.url],
            cwd=Path(__file__).resolve().parents[2],stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            wait_for(lambda:self.hub.store.snapshot('uha') and self.hub.store.snapshot('uha').task.step == 1)
            stop=threading.Event(); seen=[]
            reader=threading.Thread(target=lambda: HubClient(self.url).stream(
                lambda snap: seen.append(snap), stop=stop, read_timeout_s=5), daemon=True)
            reader.start()
            wait_for(lambda: seen)
            child.kill(); child.wait(timeout=3)
            wait_for(lambda:self.hub.store.snapshot('uha').stale,timeout=3)
            snapshots=HubClient(self.url).state()
            self.assertTrue(snapshots[0].stale)
            self.assertNotEqual(progress_view(snapshots[0]).mode,'indeterminate')
            wait_for(lambda: any(s.stale for s in seen), timeout=4)
            stop.set(); reader.join(timeout=2)
            self.assertFalse(reader.is_alive())
        finally:
            if child.poll() is None: child.kill()
            child.communicate(timeout=3)


class TestSkill(Skill):
    name='progress_probe'
    supported_methods={'UE_PYTHON'}
    def __init__(self,passed=True,observe=None): self.passed=passed; self.observe=observe
    def intent(self,params): return TaskIntent(skill=self.name,description='probe',read_only=True)
    def task_category(self,params=None): return 'general'
    def is_idempotent(self,params=None): return True
    def preflight(self,ctx,params): return {}
    def perform(self,ctx,method,params,trace):
        if self.observe:self.observe('perform')
        return {}
    def verify(self,ctx,method,params,trace):
        if self.observe:self.observe('verify')
        return VerifyReport([CheckResult('probe',self.passed)])


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        p=Path(self.tmp.name)
        cfg=Config({'router':{'stats_file':str(p/'health.json'),'quality_file':str(p/'quality.json')},
                    'workspace':{'artifact_dir':str(p/'artifacts')}})
        logger=RunLogger(p/'logs',console=False,run_name=p.name)
        self.addCleanup(lambda: [h.close() for h in logger._logger.handlers])
        router=NS(decide=lambda *a,**k:Decision('UE_PYTHON','test'),
                  available_methods=lambda **k:{'UE_PYTHON':True},feedback=lambda *a,**k:None)
        self.exe=Executor(BackendBundle(),cfg,logger,router=router)
        self.exe.controller=SessionController()
        self.hub=HubServer(port=0).start();self.addCleanup(self.hub.stop)
        self.adapter=UhaNativeAdapter(LocalPublisher(self.hub),heartbeat_s=.05)
        self.adapter.start();self.addCleanup(self.adapter.stop)
        self.adapter.attach_controller(self.exe.controller)
        self.assertTrue(attach_plan_progress(self.adapter,self.exe)['progress_hook'])
    def snap(self):
        self.adapter.flush()
        return copy.deepcopy(self.hub.store.snapshot('uha'))

    def test_real_plan_no_hundred_before_last_verification(self):
        seen=[]
        skill=TestSkill(observe=lambda stage:seen.append((stage,self.snap())))
        plan=Plan(name='three',steps=[PlanStep(skill=skill.name) for _ in range(3)])
        with patch('src.scheduler.executor.get_skill',return_value=skill):
            result=self.exe.run_plan(plan)
        self.assertTrue(result.ok)
        self.assertEqual([s.task.completed_steps for stage,s in seen if stage=='verify'],[0,1,2])
        self.assertTrue(all(progress_view(s).ratio!=1 for stage,s in seen))
        done=self.snap()
        self.assertEqual(done.status,Status.DONE)
        self.assertEqual(done.task.progress_ratio,1)
        self.assertIsNotNone(done.task.ended_at)
        old_id=done.task.id
        self.exe.run(skill,{})
        current=self.snap()
        self.assertNotEqual(current.task.id,old_id)
        self.assertIsNone(current.task.total_steps)
        self.assertIsNone(current.task.step)

    def test_verification_failure_never_completes(self):
        skill=TestSkill(False)
        with patch('src.scheduler.executor.get_skill',return_value=skill):
            result=self.exe.run_plan(Plan(name='fail',steps=[PlanStep(skill=skill.name)]))
        self.assertFalse(result.ok)
        self.assertEqual(self.snap().status,Status.ERROR)
        self.assertEqual(self.snap().task.completed_steps,0)
        self.assertFalse(any(r['type']=='task.completed' for r in self.hub.timeline('uha')))

    def test_real_fallback_counts_only_verified_step(self):
        skill=TestSkill();skill.supported_methods={'UE_PYTHON','UNREAL_MCP'}
        self.exe.router.decide=lambda *a,**k:Decision('UE_PYTHON','retry',alternatives=[{'method':'UNREAL_MCP'}])
        def verify(ctx,method,params,trace):
            self.assertEqual(self.snap().task.completed_steps,0)
            return VerifyReport([CheckResult('probe',method=='UNREAL_MCP')])
        with patch.object(skill,'verify',side_effect=verify), \
             patch('src.scheduler.executor.get_skill',return_value=skill):
            result=self.exe.run_plan(Plan(name='retry',steps=[PlanStep(skill=skill.name)]))
        self.assertTrue(result.ok)
        self.assertEqual(self.snap().task.completed_steps,1)
        self.assertEqual(self.snap().status,Status.DONE)

    def test_pause_longer_than_one_second_resumes_without_failure(self):
        self.exe.controller.begin_task('pause-probe');self.exe.controller.pause()
        results=[]
        thread=threading.Thread(target=lambda:results.append(self.exe.run(TestSkill(),{})),daemon=True)
        thread.start()
        try:
            time.sleep(1.2)
            self.assertTrue(thread.is_alive(),'Paused task must not expire after one second')
            self.exe.controller.resume();thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertTrue(results[0].ok)
        finally:
            self.exe.controller.stop();thread.join(timeout=3)

    def test_stop_unblocks_paused_executor(self):
        self.exe.controller.begin_task('pause-probe');self.exe.controller.pause()
        results=[]
        thread=threading.Thread(target=lambda:results.append(self.exe.run(TestSkill(),{})),daemon=True)
        thread.start()
        try:
            wait_for(lambda:self.snap().status==Status.PAUSED)
            self.exe.controller.stop();thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertFalse(results[0].ok)
            self.assertEqual(self.snap().status,Status.CANCELLED)
        finally:
            self.exe.controller.stop();thread.join(timeout=3)

    def test_cancel_during_execution_stays_cancelled(self):
        skill=TestSkill(observe=lambda stage:self.exe.controller.stop() if stage=='perform' else None)
        with patch('src.scheduler.executor.get_skill',return_value=skill):
            self.exe.run_plan(Plan(name='stop',steps=[PlanStep(skill=skill.name)]))
        self.assertEqual(self.snap().status,Status.CANCELLED)
        self.assertNotEqual(progress_view(self.snap()).ratio,1)

    def test_optional_failure_is_not_verified_hundred(self):
        skill=TestSkill(False)
        with patch('src.scheduler.executor.get_skill',return_value=skill):
            self.exe.run_plan(Plan(name='optional',steps=[PlanStep(skill=skill.name,optional=True)]))
        self.assertEqual(self.snap().status,Status.WARNING)
        self.assertEqual(self.snap().task.completed_steps,0)

    def test_exception_ends_task_and_stops_timer(self):
        with patch('src.scheduler.executor.get_skill',side_effect=ValueError('bad skill')):
            with self.assertRaises(ValueError):
                self.exe.run_plan(Plan(name='bad',steps=[PlanStep(skill='missing')]))
        snap=self.snap()
        self.assertEqual(snap.status,Status.ERROR)
        self.assertIsNotNone(snap.task.ended_at)

    def test_pause_persists_through_phase_updates(self):
        self.adapter.plan_begin('pause',2)
        self.exe.controller.begin_task('probe');self.exe.controller.pause()
        self.exe.controller.set_phase(TaskPhase.VERIFYING)
        self.assertEqual(self.snap().status,Status.PAUSED)

    def test_heartbeat_does_not_repeat_completed_events(self):
        self.exe.run(TestSkill(),{})
        self.adapter.flush()
        initial=sum(row['type']=='task.completed' for row in self.hub.timeline('uha'))
        self.assertEqual(initial,1)
        time.sleep(.2);self.adapter.flush()
        self.assertEqual(sum(row['type']=='task.completed' for row in self.hub.timeline('uha')),1)

    def test_dry_run_no_verified_completion(self):
        self.exe.config.data['desktop']={'dry_run':True}
        self.exe.run(TestSkill(),{})
        self.assertEqual(self.snap().status,Status.WARNING)

    def test_missing_verification_never_emits_passed(self):
        skill=TestSkill()
        with patch.object(skill,'verify',return_value=None):
            self.exe.run(skill,{})
        self.assertEqual(self.snap().status,Status.WARNING)
        self.assertFalse(any(row['type']=='verification.passed' for row in self.hub.timeline('uha')))

    def test_repeated_step_count_is_idempotent(self):
        self.adapter.plan_begin('retry',3)
        self.adapter.plan_step(1,'x')
        self.adapter.step_finished(1,True);self.adapter.step_finished(1,True)
        self.assertEqual(self.snap().task.completed_steps,1)

    def test_heartbeat_rehydrates_empty_hub_store(self):
        self.adapter.plan_begin('recover',3);self.adapter.plan_step(2,'x')
        self.adapter.step_finished(1,True);self.adapter.flush()
        self.hub.store=StateStore()
        snap=wait_for(lambda:self.hub.store.snapshot('uha'))
        self.assertEqual(snap.task.step,2)
        self.assertEqual(snap.task.completed_steps,1)

    def test_observer_failure_does_not_stop_executor(self):
        def broken(*a,**k):raise RuntimeError('broken observer')
        self.exe.add_progress_listener(broken)
        self.assertTrue(self.exe.run(TestSkill(),{}).ok)

    def test_demo_scenarios_and_unknown_total_reset(self):
        from uah.tools.progress_demo import run_demo
        for scenario,expected in (('success',Status.DONE),('failure',Status.ERROR),('unknown',Status.DONE)):
            run_demo(self.adapter,self.exe.controller,scenario,.001)
            self.assertEqual(self.snap().status,expected)
        self.assertIsNone(self.snap().task.total_steps)


if __name__=='__main__':
    unittest.main(verbosity=2)
