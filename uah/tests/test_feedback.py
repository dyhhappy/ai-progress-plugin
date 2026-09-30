"""Feedback integration through real reporter processes, with no model calls."""
import json
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

from uah.adapters.feedback import FeedbackChannel, codex_hook_arguments
from uah.adapters.process import ProcessAgent
from uah.tools.report_progress import hook_event, instructions, read_hook_input
import io
from uah.core.models import AgentSnapshot, AgentRef, TaskState, Status
import os
from uah.tests import test_process_wrapper as helpers
from uah.tests.test_process_wrapper import wait_for
from uah.core.transport import HubClient
from uah.ui.progress import progress_view

SCRIPT=Path(__file__).resolve().parents[1]/'tools/report_progress.py'


class FeedbackTests(unittest.TestCase):
    def test_activity_display_and_tool_events(self):
        runner=ProcessAgent([sys.executable],feedback='codex')
        self.assertEqual(runner.state['payload']['runtime']['display_mode'],'activity')
        runner.accept_feedback({'type':'turn_started','turn_id':'stage'})
        for name,prefix in (('PreToolUse','正在执行'),('PostToolUse','操作已结束')):
            event=hook_event({'hook_event_name':name,'turn_id':'stage','tool_name':'Bash','tool_input':{'command':'echo hello'}})
            runner.accept_feedback(event)
            self.assertIn(prefix,runner.state['activity']['summary'])
        runner.accept_feedback({'type':'estimate','turn_id':'stage','message':'正在核对结果'})
        self.assertEqual(runner.state['activity']['summary'],'正在核对结果')
        self.assertIsNone(hook_event({'hook_event_name':'PostToolUse','tool_name':'Bash',
            'tool_input':{'command':'python report_progress.py --stage check'}}))
        snap=AgentSnapshot(AgentRef(id='test'),status=Status.RUNNING,task=TaskState(total_steps=3,completed_steps=1),
            runtime={'display_mode':'activity','feedback_connected':True,'estimated_percent':50})
        snap.activity.summary='正在核对结果'
        view=progress_view(snap)
        self.assertEqual(view.text,'正在核对结果')
        self.assertIsNone(view.ratio)
        self.assertNotIn('%',view.text)
        snap.status=Status.DONE;snap.activity.summary='本轮回答已结束'
        self.assertEqual(progress_view(snap).mode,'hidden')

    def test_public_stage_survives_tools_and_resets_next_turn(self):
        runner=ProcessAgent([sys.executable],feedback='codex')
        runner.accept_feedback({'type':'turn_started','turn_id':'one'})
        runner.accept_feedback({'type':'activity','turn_id':'one','source':'public-commentary','message':'正在编写科普'})
        for name in ('PreToolUse','PostToolUse'):
            event=hook_event({'hook_event_name':name,'turn_id':'one','tool_name':'web.run','tool_input':{'search_query':[{'q':'private query'}]}})
            runner.accept_feedback(event)
            self.assertEqual(runner.state['activity']['summary'],'正在编写科普')
            self.assertIn('搜索资料',runner.state['activity']['detail'])
            self.assertNotIn('private query',str(event))
        runner.accept_feedback({'type':'turn_started','turn_id':'two'})
        event=hook_event({'hook_event_name':'PreToolUse','turn_id':'two','tool_name':'webrun','tool_input':{'open':[{'ref_id':'private-url'}]}})
        runner.accept_feedback(event)
        self.assertEqual(runner.state['activity']['summary'],'正在阅读网页资料')
        runner.accept_feedback({'type':'turn_finished','turn_id':'two'})
        runner.accept_feedback(event)
        self.assertEqual(runner.state['status'],'DONE')

    def test_plain_questions_receive_automatic_context_each_turn(self):
        channel=FeedbackChannel()
        try:
            for turn,prompt in (('first','帮我制定英语学习计划'),('second','再简短一点')):
                raw={'hook_event_name':'UserPromptSubmit','turn_id':turn,'prompt':prompt}
                result=subprocess.run([sys.executable,str(SCRIPT),'--channel',str(channel.path),'--hook'],
                    input=json.dumps(raw),capture_output=True,text=True,encoding='utf-8')
                self.assertEqual(result.returncode,0,result.stderr)
                context=json.loads(result.stdout)['hookSpecificOutput']['additionalContext']
                self.assertIn('无需用户提醒',context)
                self.assertIn('不需要百分比',context)
                self.assertIn('--turn-id',context)
                self.assertIn(turn,context)
                self.assertNotIn('短回答不必增加工具调用',context)
            self.assertEqual([e['turn_id'] for e in channel.events()],['first','second'])
        finally:channel.close()

    def test_bom_and_large_prompt_input(self):
        raw={'hook_event_name':'UserPromptSubmit','prompt':'x'*200000}
        self.assertEqual(read_hook_input(io.StringIO('\ufeff'+json.dumps(raw))),raw)
        for invalid in ('','\ufeff','[]','not JSON'):
            with self.assertRaises(ValueError):read_hook_input(io.StringIO(invalid))

    def test_step_reporter_without_update_plan(self):
        channel=FeedbackChannel()
        try:
            result=subprocess.run([sys.executable,str(SCRIPT),'--channel',str(channel.path),
                '--completed','1','--total','3','--stage','第一步完成'],capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            event=list(channel.events())[0]
            self.assertEqual((event['type'],event['completed'],event['total']),('plan',1,3))
        finally:channel.close()

    @unittest.skipUnless(os.name=='nt','Windows hook invocation')
    def test_exact_configured_hook_command_roundtrip(self):
        import tomllib
        channel=FeedbackChannel()
        try:
            args=codex_hook_arguments()
            for index,name in ((1,'SessionStart'),(3,'UserPromptSubmit'),(5,'PostToolUse'),(9,'Stop')):
                cmd=tomllib.loads(args[index])['hooks'][name][0]['hooks'][0]['command']
                raw={'hook_event_name':name,'turn_id':'round-exact','tool_name':'update_plan',
                     'tool_input':{'plan':[{'status':'completed'},{'status':'in_progress'}]}}
                result=subprocess.run(cmd,input='\ufeff'+json.dumps(raw),capture_output=True,text=True,encoding='utf-8',
                    env={**os.environ,'UAH_REPORT_CHANNEL':str(channel.path)},timeout=3)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(result.stderr,'')
                self.assertIsInstance(json.loads(result.stdout),dict)
            events=list(channel.events())
            self.assertEqual([e['type'] for e in events],['ready','turn_started','plan','turn_finished'])
            self.assertEqual(events[2]['completed'],1)
            self.assertEqual(events[2]['total'],2)
        finally:channel.close()

    @unittest.skipUnless(os.name=='nt','PowerShell command')
    def test_generated_powershell_instruction_executes(self):
        channel=FeedbackChannel()
        try:
            command=instructions(channel.path,'round-test').splitlines()[1]
            result=subprocess.run(['powershell','-NoProfile','-NonInteractive','-Command',command],capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            event=list(channel.events())[0]
            self.assertIsNone(event['percent'])
            self.assertEqual(event['message'],'正在整理回答')
            self.assertEqual(event['turn_id'],'round-test')
        finally:channel.close()

    def test_visible_progress_lifecycle_and_missing_hook_hint(self):
        snap=AgentSnapshot(AgentRef(id='test'),status=Status.RUNNING,task=TaskState(),
            runtime={'source':'cli-wrapper','feedback':'codex','process_running':True})
        self.assertIn('反馈未连接',progress_view(snap).text)
        snap.runtime['feedback_connected']=True
        self.assertEqual(progress_view(snap).ratio,0)
        snap.runtime['estimated_percent']=37
        self.assertIn('37%',progress_view(snap).text)
        self.assertIn('AI 估计',progress_view(snap).text)
        snap.task.total_steps=3;snap.task.completed_steps=3
        self.assertEqual(progress_view(snap).ratio,.99)
        snap.status=Status.DONE
        self.assertEqual(progress_view(snap).ratio,1)
        self.assertIn('100%',progress_view(snap).text)

    def test_reporter_and_channel_isolation(self):
        a,b=FeedbackChannel(),FeedbackChannel()
        try:
            result=subprocess.run([sys.executable,str(SCRIPT),'--channel',str(a.path),'--percent','35','--stage','分析'],capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(list(a.events())[0]['percent'],35)
            self.assertEqual(list(b.events()),[])
            result=subprocess.run([sys.executable,str(SCRIPT),'--channel',str(a.path),'--percent','nan'],capture_output=True)
            self.assertEqual(result.returncode,2)
            self.assertEqual(list(a.events()),[])
        finally:a.close();b.close()

    def test_hooks_advisory_and_ignore_subagents(self):
        channel=FeedbackChannel()
        try:
            for raw in ({'hook_event_name':'UserPromptSubmit','turn_id':'abc'},
                        {'hook_event_name':'Stop','turn_id':'abc'},
                        {'hook_event_name':'Stop','agent_id':'child'}):
                result=subprocess.run([sys.executable,str(SCRIPT),'--channel',str(channel.path),'--hook'],input=json.dumps(raw),capture_output=True,text=True,encoding='utf-8')
                self.assertEqual(result.returncode,0)
                self.assertEqual(result.stderr,'')
            self.assertEqual([e['type'] for e in channel.events()],['turn_started','turn_finished'])
        finally:channel.close()

    def test_stale_round_and_estimate_validation(self):
        runner=ProcessAgent([sys.executable])
        for turn in ('one','two'):
            runner.accept_feedback({'type':'turn_started','turn_id':turn})
            runner.accept_feedback({'type':'turn_finished','turn_id':turn})
        runner.accept_feedback({'type':'turn_started','turn_id':'one'})
        self.assertEqual(runner.turn_id,'two')
        self.assertEqual(runner.state['status'],'DONE')
        runner.accept_feedback({'type':'turn_started','turn_id':'three'})
        for value in (True,100,-1,float('nan')):
            runner.accept_feedback({'type':'estimate','percent':value})
        self.assertIsNone(runner.state['payload']['runtime']['estimated_percent'])

    def test_hook_configuration_is_valid_toml(self):
        import tomllib
        args=codex_hook_arguments()
        for arg in args[1::2]:
            parsed=tomllib.loads(arg)
            self.assertIn('hooks',parsed)
        self.assertIsNone(hook_event({'hook_event_name':'Stop','subagent_id':'child'}))


class LiveFeedbackTests(unittest.TestCase):
    setUp=helpers.IntegrationTests.setUp
    runner=helpers.IntegrationTests.runner
    snapshot=helpers.IntegrationTests.snapshot
    run_quiet=helpers.IntegrationTests.run_quiet

    def test_round_completes_while_process_alive_and_stop_is_prompt(self):
        runner=self.runner('import time;time.sleep(30)')
        thread=threading.Thread(target=lambda:self.run_quiet(runner))
        thread.start()
        try:
            wait_for(lambda:runner.state['payload']['runtime']['process_running'])
            def report(*args):
                result=subprocess.run([sys.executable,str(SCRIPT),'--channel',str(runner.feedback_channel.path),'--turn-id','round-1',*args],capture_output=True)
                self.assertEqual(result.returncode,0,result.stderr)
            report('--start')
            report('--percent','42','--stage','实现中')
            wait_for(lambda:runner.state['payload']['runtime']['estimated_percent']==42)
            wait_for(lambda:'AI 估计' in progress_view(self.snapshot(runner)).text)
            report('--done')
            wait_for(lambda:self.snapshot(runner).status.value=='DONE')
            self.assertIsNone(runner.proc.poll())
            client=HubClient(self.hub.url())
            started=time.monotonic()
            receipt=client.submit_control('stop',agent_id=runner.agent_id,token=self.hub.approval_token)
            wait_for(lambda:runner.proc.poll() is not None,seconds=3)
            self.assertLess(time.monotonic()-started,3)
            wait_for(lambda:client._get('/control/result?id='+receipt['id']).get('state')=='applied')
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(self.snapshot(runner).status.value,'CANCELLED')
        finally:
            if runner.proc and runner.proc.poll() is None:runner.stop_process()
            thread.join(5)


if __name__=='__main__':unittest.main(verbosity=2)
