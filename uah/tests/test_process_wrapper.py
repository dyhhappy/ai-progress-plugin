"""Real child processes and HTTP Hub; no AI credentials, UE or desktop automation."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from uah.adapters.process import ProcessAgent, PREFIX, clean_text, resolve_command
from uah.core.transport import HubServer, HubClient
from uah.tools.agent_launch import load_profiles, main
from uah.ui.progress import progress_view


def wait_for(predicate,seconds=5):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        if predicate(): return
        time.sleep(.03)
    raise AssertionError('timeout')


class ParserTests(unittest.TestCase):
    def wrapper(self): return ProcessAgent([sys.executable],mode='capture')

    def test_plain_output_never_infers_completion(self):
        runner=self.wrapper()
        runner.accept_line('SUCCESS 100% all done token=secret')
        self.assertFalse(runner.reported_success)
        self.assertIsNone(runner.state['task']['total_steps'])
        self.assertNotIn('secret',str(runner.state))

    def test_explicit_progress_and_waiting(self):
        runner=self.wrapper()
        runner.accept_line(PREFIX+json.dumps({'type':'progress','completed':1,'total':3}))
        self.assertEqual(runner.state['task']['completed_steps'],1)
        runner.accept_line(PREFIX+json.dumps({'type':'waiting_input'}))
        self.assertEqual(runner.state['status'],'WAITING_INPUT')
        runner.accept_line(PREFIX+json.dumps({'type':'running'}))
        self.assertEqual(runner.state['status'],'RUNNING')

    def test_bad_and_forged_events_ignored(self):
        runner=self.wrapper()
        for line in ('{', json.dumps({'type':'progress','completed':True,'total':3}),
                     json.dumps({'type':'progress','completed':9,'total':2}), '[]'):
            runner.accept_line(PREFIX+line)
        self.assertIsNone(runner.state['task']['completed_steps'])
        runner.accept_line(PREFIX+json.dumps({'type':'activity','agent':'someone-else',
                                            'runtime':{'control_actions':['pause']}}))
        self.assertNotEqual(runner.agent_id,'someone-else')
        self.assertEqual(runner.state['payload']['runtime']['control_actions'],['stop'])

    def test_failure_cannot_be_overwritten(self):
        runner=self.wrapper()
        runner.accept_line(PREFIX+'{"type":"failed"}')
        runner.accept_line(PREFIX+'{"type":"completed"}')
        self.assertTrue(runner.reported_error)
        self.assertFalse(runner.reported_success)

    def test_failure_after_completion_overrides_success(self):
        runner=self.wrapper()
        runner.accept_line(PREFIX+'{"type":"completed"}')
        runner.accept_line(PREFIX+'{"type":"failed"}')
        self.assertTrue(runner.reported_error)
        self.assertEqual(runner.state['status'],'BLOCKED')

    def test_relative_script_uses_selected_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            script=Path(directory)/'local_agent.py'
            script.write_text('print("hello")',encoding='utf-8')
            command=resolve_command(['./local_agent.py'],directory)
            self.assertEqual(command,[sys.executable,'-u',str(script.resolve())])

    def test_redaction_and_control_characters(self):
        text=clean_text('\x1b[31mhello\x00 password=abc Bearer abcdef sk-abcdefghijklmnopqrstuv')
        self.assertNotIn('\x1b',text);self.assertNotIn('abc',text)
        self.assertIn('hello',text)

    def test_profile_validation_relative_directory_and_unicode(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);p=root/'agents.json'
            p.write_text(json.dumps({'测试':{'command':[sys.executable,'-u'],'cwd':'.','mode':'capture'}}),encoding='utf-8')
            self.assertEqual(load_profiles(p)['测试']['cwd'],str(root.resolve()))
            p.write_text('{"bad":{"command":"echo hi"}}')
            with self.assertRaises(ValueError):load_profiles(p)

    def test_missing_executable_and_invalid_mode(self):
        with self.assertRaises(FileNotFoundError):resolve_command(['no-such-uah-command-123'],os.getcwd())
        with self.assertRaises(ValueError):ProcessAgent([sys.executable],mode='fake')

    def test_same_executable_gets_distinct_identity(self):
        self.assertNotEqual(self.wrapper().agent_id,self.wrapper().agent_id)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.hub=HubServer(port=0).start()
        self.addCleanup(self.hub.stop)

    def runner(self,code,**kwargs):
        return ProcessAgent([sys.executable,'-u','-c',code],url=self.hub.url(),
                            mode='capture',**kwargs)

    def run_quiet(self,runner):
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            return runner.run()

    def snapshot(self,runner):
        return next(s for s in HubClient(self.hub.url()).state() if s.agent.id==runner.agent_id)

    def test_real_process_zero_is_unverified_not_done(self):
        runner=self.runner('print("all done")')
        self.assertEqual(self.run_quiet(runner),0)
        snap=self.snapshot(runner)
        self.assertEqual(snap.status.value,'WARNING')
        self.assertIsNone(snap.task.progress_ratio)
        self.assertIsNotNone(snap.task.ended_at)

    def test_reported_progress_success_and_label(self):
        code='print(\'UAH_EVENT {"type":"progress","completed":3,"total":3}\');print(\'UAH_EVENT {"type":"completed"}\')'
        runner=self.runner(code)
        self.assertEqual(self.run_quiet(runner),0)
        snap=self.snapshot(runner)
        self.assertEqual(snap.status.value,'DONE')
        self.assertEqual(snap.task.progress_ratio,1)
        self.assertIn('已上报',progress_view(snap).text)

    def test_nonzero_overrides_reported_success(self):
        runner=self.runner('import sys;print(\'UAH_EVENT {"type":"completed"}\');sys.exit(7)')
        self.assertEqual(self.run_quiet(runner),7)
        self.assertEqual(self.snapshot(runner).status.value,'ERROR')

    def test_reported_failure_with_zero_exit(self):
        runner=self.runner('print(\'UAH_EVENT {"type":"failed"}\')')
        self.assertEqual(self.run_quiet(runner),0)
        self.assertEqual(self.snapshot(runner).status.value,'ERROR')

    def test_stderr_cannot_forge_protocol(self):
        runner=self.runner('import sys;print(\'UAH_EVENT {"type":"completed"}\',file=sys.stderr)')
        self.run_quiet(runner)
        self.assertEqual(self.snapshot(runner).status.value,'WARNING')

    def test_unicode_paths_and_no_newline_and_invalid_bytes(self):
        with tempfile.TemporaryDirectory(prefix='UAH 测试 ') as temp:
            runner=self.runner('import os;os.write(1,b"x"*100000+b"\\xff");os.write(2,b"stderr")',cwd=temp)
            self.assertEqual(self.run_quiet(runner),0)
            self.assertGreater(runner.lines,1)
            self.assertEqual(self.snapshot(runner).status.value,'WARNING')

    def test_missing_command_emits_error(self):
        runner=ProcessAgent(['uah-command-does-not-exist'],url=self.hub.url())
        self.assertEqual(self.run_quiet(runner),127)
        self.assertEqual(self.snapshot(runner).status.value,'ERROR')

    def test_disconnected_hub_does_not_block_child(self):
        runner=self.runner('print("ok")')
        self.hub.stop()
        start=time.monotonic()
        self.assertEqual(self.run_quiet(runner),0)
        self.assertLess(time.monotonic()-start,8)

    def test_running_child_recovers_full_snapshot_after_hub_restart(self):
        runner=self.runner('import time;print(\'UAH_EVENT {"type":"progress","completed":1,"total":3}\',flush=True);time.sleep(20)')
        result=[]
        thread=threading.Thread(target=lambda:result.append(self.run_quiet(runner)))
        thread.start()
        try:
            wait_for(lambda:runner.state['task'].get('completed_steps')==1)
            port=self.hub.port
            self.hub.stop()
            self.hub=HubServer(port=port).start()
            self.addCleanup(self.hub.stop)
            wait_for(lambda:any(s.agent.id==runner.agent_id and s.task.completed_steps==1
                                for s in HubClient(self.hub.url()).state()))
            self.assertIsNone(runner.proc.poll())
        finally:
            if runner.proc is not None and runner.proc.poll() is None:runner.stop_process()
            thread.join(timeout=8)
        self.assertFalse(thread.is_alive())

    @unittest.skipUnless(os.name=='nt','Windows launcher exit status')
    def test_cmd_entry_preserves_exit_status(self):
        root=Path(__file__).resolve().parents[2]
        command=resolve_command([str(root/'launch_agent.cmd'),'--url',self.hub.url(),
                                 '--sample','failure'],str(root))
        child=subprocess.run(command,cwd=root,capture_output=True,text=True,encoding='utf-8',
                             errors='replace',timeout=15)
        self.assertEqual(child.returncode,7,child.stdout+child.stderr)

    def test_http_control_rejects_pause_and_stops_owned_tree(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'child.pid'
            code=('import subprocess,sys,time,pathlib;time.sleep(.2);'
                  'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"]);'
                  f'pathlib.Path({str(path)!r}).write_text(str(p.pid));time.sleep(30)')
            runner=self.runner(code)
            result=[]
            thread=threading.Thread(target=lambda:result.append(self.run_quiet(runner)))
            thread.start()
            try:
                wait_for(path.exists)
                child_pid=int(path.read_text())
                client=HubClient(self.hub.url())
                pause=client.submit_control('pause',agent_id=runner.agent_id,token=self.hub.approval_token)
                wait_for(lambda:client._get('/control/result?id='+pause['id']).get('state')=='failed')
                self.assertIsNone(runner.proc.poll())
                stop=client.submit_control('stop',agent_id=runner.agent_id,token=self.hub.approval_token)
                thread.join(timeout=8)
                self.assertFalse(thread.is_alive())
                self.assertEqual(result,[130])
                self.assertEqual(self.snapshot(runner).status.value,'CANCELLED')
                wait_for(lambda:client._get('/control/result?id='+stop['id']).get('state')=='applied')
                if os.name=='nt':
                    import ctypes
                    k=ctypes.WinDLL('kernel32',use_last_error=True)
                    k.OpenProcess.restype=ctypes.c_void_p
                    handle=k.OpenProcess(0x1000,False,child_pid)
                    if handle:
                        try:
                            exit_code=ctypes.c_ulong()
                            k.GetExitCodeProcess.argtypes=[ctypes.c_void_p,ctypes.c_void_p]
                            self.assertTrue(k.GetExitCodeProcess(handle,ctypes.byref(exit_code)))
                            self.assertNotEqual(exit_code.value,259)
                        finally:
                            k.CloseHandle.argtypes=[ctypes.c_void_p];k.CloseHandle(handle)
            finally:
                if runner.proc is not None and runner.proc.poll() is None:runner.stop_process()
                thread.join(timeout=5)

    def test_cli_direct_command_without_gui(self):
        with contextlib.redirect_stdout(io.StringIO()):
            result=main(['--url',self.hub.url(),'--mode','capture','--',sys.executable,'-c','print("hi")'])
        self.assertEqual(result,0)

    def test_terminal_mode_preserves_input_and_output(self):
        code=('from uah.adapters.process import ProcessAgent;import sys;'
              f'r=ProcessAgent([sys.executable,"-u","-c","print(input())"],url={self.hub.url()!r},mode="terminal");'
              'raise SystemExit(r.run())')
        child=subprocess.run([sys.executable,'-c',code],input='hello from terminal\n',
            capture_output=True,text=True,timeout=10)
        self.assertEqual(child.returncode,0,child.stderr)
        self.assertIn('hello from terminal',child.stdout)

    def test_other_process_survives_stop(self):
        unrelated=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
        try:
            runner=self.runner('import time;time.sleep(30)')
            result=[]
            thread=threading.Thread(target=lambda:result.append(self.run_quiet(runner)))
            thread.start()
            try:
                wait_for(lambda:runner.proc is not None)
                runner.stop_process()
                thread.join(timeout=8)
                self.assertFalse(thread.is_alive())
                self.assertIsNone(unrelated.poll())
                self.assertEqual(result,[130])
            finally:
                if runner.proc is not None and runner.proc.poll() is None:runner.stop_process()
                thread.join(timeout=5)
        finally:
            unrelated.terminate();unrelated.wait(timeout=3)

    @unittest.skipUnless(os.name=='nt','Windows batch launcher')
    def test_batch_program_in_unicode_path(self):
        with tempfile.TemporaryDirectory(prefix='UAH 脚本 ') as directory:
            path=Path(directory)/'test agent.cmd'
            path.write_bytes(b'@echo off\r\necho ordinary output\r\nexit /b 7\r\n')
            runner=ProcessAgent([str(path)],url=self.hub.url(),mode='capture')
            self.assertEqual(self.run_quiet(runner),7)
            self.assertEqual(self.snapshot(runner).status.value,'ERROR')


if __name__=='__main__':unittest.main(verbosity=2)
