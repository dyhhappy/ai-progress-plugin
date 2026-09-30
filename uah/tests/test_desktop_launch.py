import json
import tempfile
import unittest
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch
from uah.tools.desktop_launch import install_hooks,DesktopMonitor,MARKER,save_app,load_apps
from uah.adapters.feedback import write_event
from uah.core.transport import HubServer,HubClient


class DesktopTests(unittest.TestCase):
    @unittest.skipUnless(os.name=='nt','Windows pythonw launcher')
    def test_gui_and_console_generate_identical_hooks(self):
        with tempfile.TemporaryDirectory() as directory:
            channel=install_hooks(directory)
            path=Path(directory)/'.codex/hooks.json'
            console=path.read_text(encoding='utf-8')
            with patch('sys.executable',str(Path(sys.executable).with_name('pythonw.exe'))):
                install_hooks(directory)
            self.assertEqual(path.read_text(encoding='utf-8'),console)

    @unittest.skipUnless(os.name=='nt','Windows desktop hook')
    def test_installed_hook_and_manual_stage_keep_session_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            channel=install_hooks(directory)
            monitor=DesktopMonitor(channel,'http://127.0.0.1:1')
            try:
                data=json.loads((Path(directory)/'.codex/hooks.json').read_text(encoding='utf-8'))
                command=data['hooks']['UserPromptSubmit'][0]['hooks'][0]['command']
                raw={'hook_event_name':'UserPromptSubmit','session_id':'session-a','turn_id':'turn-a'}
                result=subprocess.run(command,input='\ufeff'+json.dumps(raw),capture_output=True,text=True,encoding='utf-8',timeout=5)
                self.assertEqual(result.returncode,0,result.stderr)
                context=json.loads(result.stdout)['hookSpecificOutput']['additionalContext']
                self.assertIn('--session-id session-a',context)
                stage_command=context.splitlines()[1]
                result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',stage_command],capture_output=True,timeout=5)
                self.assertEqual(result.returncode,0,result.stderr)
                events=list(monitor.mailbox.events())
                self.assertEqual([e['session_id'] for e in events],['session-a','session-a'])
                self.assertEqual(events[-1]['message'],'正在整理回答')
                monitor.close()
                result=subprocess.run(command,input=json.dumps(raw),capture_output=True,text=True,encoding='utf-8',timeout=5)
                self.assertEqual(json.loads(result.stdout),{})
                self.assertEqual(list(monitor.mailbox.events()),[])
            finally:monitor.close()

    def test_custom_app_paths_are_saved_without_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);exe=root/'Agent 测试.exe';exe.touch();config=root/'apps.json'
            save_app(str(exe),'launch-only',config)
            save_app(str(exe),'codex',config)
            self.assertEqual(load_apps(config),[{'path':str(exe.resolve()),'adapter':'codex'}])
            with self.assertRaises(ValueError):save_app(str(root/'missing.exe'),'codex',config)

    def test_project_hooks_preserve_existing_and_are_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'.codex/hooks.json';path.parent.mkdir()
            original={'hooks':{'Stop':[{'hooks':[{'type':'command','command':'echo mine'}]}]}}
            path.write_text(json.dumps(original))
            channel=install_hooks(root)
            first=path.read_text(encoding='utf-8');install_hooks(root)
            self.assertEqual(first,path.read_text(encoding='utf-8'))
            data=json.loads(first)
            self.assertEqual(data['hooks']['Stop'][0]['hooks'][0]['command'],'echo mine')
            self.assertEqual(len(data['hooks']['Stop']),2)
            self.assertTrue(channel.is_dir())
            self.assertTrue(path.with_name('hooks.before-uah-desktop.json').exists())

    def test_invalid_existing_config_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'.codex/hooks.json';path.parent.mkdir();path.write_text('[]')
            with self.assertRaises(ValueError):install_hooks(directory)
            self.assertEqual(path.read_text(),'[]')

    def test_sessions_are_separate_and_have_no_process_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            channel=install_hooks(directory)
            hub=HubServer(port=0).start()
            try:
                monitor=DesktopMonitor(channel,hub.url())
                for session in ('a','b'):
                    monitor.accept({'type':'turn_started','turn_id':'turn-'+session,'session_id':session,'source':'codex-hook'})
                monitor.accept({'type':'activity','turn_id':'turn-a','session_id':'a','message':'正在制作 PPT'})
                monitor.accept({'type':'turn_finished','turn_id':'turn-b','session_id':'b'})
                self.assertEqual(monitor.agents['a'].state['activity']['summary'],'正在制作 PPT')
                self.assertEqual(monitor.agents['b'].state['status'],'DONE')
                for snap in HubClient(hub.url()).state():
                    self.assertFalse(snap.runtime['soft_control'])
                    self.assertEqual(snap.runtime['control_actions'],[])
            finally:
                monitor.close()
                hub.stop()

if __name__=='__main__':unittest.main(verbosity=2)
