"""Render hidden Tk widgets only; no desktop capture, input or window activation."""
import os
import tempfile
import unittest
import tkinter as tk
import time
from unittest.mock import patch
from pathlib import Path
from uah.hosts.desktop.compact import HudApp
from uah.safety.bridge import SAFETY_AGENT_ID
from uah.core.models import AgentSnapshot, AgentRef, TaskState, Status
from uah.ui.notify import Notifier
from uah.core.attention import Attention


class HiddenWidgets(unittest.TestCase):
    def test_forms_dpi_progress_icons_and_settings(self):
        for scale in ('1','1.25','1.5','2'):
            with self.subTest(scale=scale), tempfile.TemporaryDirectory() as tmp:
                root=tk.Tk();root.withdraw()
                with patch.dict(os.environ,{'UAH_HUD_SCALE':scale}), \
                     patch('tkinter.Tk',return_value=root), \
                     patch.object(root,'deiconify'), \
                     patch.object(HudApp,'_foreground_hwnd',return_value=0), \
                     patch.object(HudApp,'_no_activate'), \
                     patch.object(HudApp,'_restore_foreground',return_value=False):
                    app=HudApp(settings_path=str(Path(tmp)/'window.json'),
                               notifier=Notifier(sinks=[],state_path=str(Path(tmp)/'notify.json')))
                    try:
                        app.build()
                        self.assertEqual(root.state(),'withdrawn')
                        snap=AgentSnapshot(AgentRef(id='uha'),status=Status.RUNNING,
                            task=TaskState(id='task',step=2,total_steps=3,completed_steps=1),
                            runtime={'task_domain':'ExampleProject','dry_run':False,
                                     'approval':'未接入','soft_control':True})
                        app._snapshots={'uha':snap};app._stream_status='connected'
                        for mode in app.MODES:
                            app._apply_form(mode,save=False)
                            for tab in ('task','method','settings'):
                                app._set_tab(tab,save=False)
                                app._render();root.update_idletasks()
                                self.assertLessEqual(app._forms[mode].winfo_reqwidth(),
                                    round(app.SIZES[mode][0]*float(scale)), (mode,tab,scale))
                            if mode!='mini':
                                track,thumb,label=app._progress_widgets[mode]
                                self.assertAlmostEqual(float(thumb.place_info()['relwidth']),1/3,places=4)
                                self.assertIn('33%',label.cget('text'))
                        self.assertEqual(app._settings_widgets['domain'].cget('text'),'ExampleProject')
                        with patch.object(app.client,'submit_control',return_value={'ok':True,'id':'test-request'}):
                            app._control('pause')
                            deadline=time.monotonic()+2
                            while app._inbox.empty() and time.monotonic()<deadline:time.sleep(.01)
                            app._drain()
                            self.assertEqual(app._control_pending['id'],'test-request')
                        with patch.object(app.client,'_get',return_value={'state':'applied','message':'暂停已确认'}):
                            app._check_control()
                            deadline=time.monotonic()+2
                            while app._inbox.empty() and time.monotonic()<deadline:time.sleep(.01)
                            app._drain()
                            self.assertIsNone(app._control_pending)
                            self.assertIn('暂停已确认',app._control_message)
                        app._control_message_until=0;app._render()
                        root.geometry('452x104+-1800+-200')
                        self.assertTrue(all(b.cget('image') for b in app._pause_buttons if hasattr(b,'_icon_key')))
                        snap.stale=True;app._render()
                        self.assertIn('离线',app._progress_widgets['compact'][2].cget('text'))
                        snap.stale=False;snap.status=Status.ERROR
                        snap.attention=Attention.L4;app._render();app._update_alert()
                        self.assertGreater(app._alert_px_height,0)
                        snap.status=Status.DONE;snap.task.completed_steps=3;app._render()
                        self.assertEqual(float(app._progress_widgets['compact'][1].place_info()['relwidth']),1)
                        with patch.dict(os.environ,{'UAH_HUD_SCALE':'1.75'}):
                            app._geometry_check_at=0
                            app._check_display();app._render();root.update_idletasks()
                            self.assertEqual(app._scale,1.75)
                            self.assertEqual(len(app._progress_widgets),2)
                            self.assertEqual(root.state(),'withdrawn')
                        app._snapshots={SAFETY_AGENT_ID:AgentSnapshot(AgentRef(id=SAFETY_AGENT_ID),
                            status=Status.ERROR,attention=Attention.L5)}
                        app._alert_dismissed_until=time.time()+60
                        app._apply_form('mini',save=False);app._update_alert();root.update_idletasks()
                        self.assertEqual(app._alert_level,5)
                        self.assertIsNone(app._current())
                        self.assertGreaterEqual(app._scaled_size()[0],round(app.SIZES['compact'][0]*app._scale))
                    finally:
                        app.close()


if __name__=='__main__':unittest.main(verbosity=2)
