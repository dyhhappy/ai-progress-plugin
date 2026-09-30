"""Open the installed desktop app with project-scoped UAH hooks and a HUD."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import tomllib

from ..adapters.feedback import codex_hook_arguments, FeedbackChannel
from ..adapters.process import ProcessAgent

MARKER='UAH desktop activity'
SETTINGS=Path(__file__).resolve().parents[2]/'.state/uah/desktop-apps.json'


def load_apps(path=SETTINGS):
    if not path.exists():return []
    data=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data,list):raise ValueError('应用配置必须是列表')
    for item in data:
        if not isinstance(item,dict) or not isinstance(item.get('path'),str) or item.get('adapter') not in ('codex','launch-only'):
            raise ValueError('应用配置格式错误')
    return data


def save_app(executable,adapter,path=SETTINGS):
    target=Path(executable).expanduser().resolve()
    if not target.is_file() or target.suffix.lower() not in ('.exe','.lnk'):
        raise ValueError('请选择桌面程序的 exe 或快捷方式 lnk 文件')
    if adapter not in ('codex','launch-only'):raise ValueError('不支持的接入类型')
    items=[a for a in load_apps(path) if os.path.normcase(a['path'])!=os.path.normcase(str(target))]
    items.append({'path':str(target),'adapter':adapter})
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(items,ensure_ascii=False,indent=2),encoding='utf-8')
    os.replace(temporary,path)
    return items


def discover_apps():
    if os.name!='nt':return []
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',
        "[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); Get-StartApps | Where-Object {$_.Name -match '^(ChatGPT|Codex)$'} | ConvertTo-Json -Compress"],
        capture_output=True,text=True,encoding='utf-8',timeout=15,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode:raise OSError('无法读取已安装应用')
    data=json.loads(result.stdout.strip() or '[]')
    return data if isinstance(data,list) else [data]


def install_hooks(workspace):
    workspace=Path(workspace).resolve()
    if not workspace.is_dir():raise ValueError('请选择存在的工作目录')
    channel=workspace/'.uah/uah-feedback-desktop'
    channel.mkdir(parents=True,exist_ok=True)
    path=workspace/'.codex/hooks.json'
    data=json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    if not isinstance(data,dict) or not isinstance(data.get('hooks',{}),dict):raise ValueError('现有 hooks.json 格式不支持，未覆盖')
    hooks=data.setdefault('hooks',{})
    args=codex_hook_arguments(channel)
    for value in args[1::2]:
        event,groups=next(iter(tomllib.loads(value)['hooks'].items()))
        existing=hooks.get(event,[])
        if not isinstance(existing,list):raise ValueError('现有钩子格式不支持，未覆盖')
        kept=[]
        for group in existing:
            if not isinstance(group,dict) or not isinstance(group.get('hooks'),list):raise ValueError('现有钩子格式不支持，未覆盖')
            remaining=[h for h in group['hooks'] if not isinstance(h,dict) or h.get('statusMessage')!=MARKER]
            if remaining:kept.append({**group,'hooks':remaining})
        for group in groups:
            for handler in group['hooks']:handler['statusMessage']=MARKER
        hooks[event]=kept+groups
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        backup=path.with_name('hooks.before-uah-desktop.json')
        if not backup.exists():backup.write_bytes(path.read_bytes())
    temporary=path.with_suffix('.uah.tmp')
    temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    os.replace(temporary,path)
    return channel


class DesktopMonitor:
    def __init__(self,channel,url):
        self.channel=Path(channel)
        self.url=url
        self.stop=threading.Event()
        self.agents={}
        self.lockfile=None
        if os.name=='nt':
            import msvcrt
            self.lockfile=(Path(channel)/'monitor.lock').open('a+b')
            self.lockfile.seek(0)
            try:msvcrt.locking(self.lockfile.fileno(),msvcrt.LK_NBLCK,1)
            except OSError:
                self.lockfile.close()
                raise ValueError('此工作目录已经接入另一个 UAH 窗口，请先关闭那个窗口')
        self.mailbox=object.__new__(FeedbackChannel)
        self.mailbox.path=Path(channel)
        list(self.mailbox.events()) # Old runs must not be replayed as current work.
        (self.channel/'listener.alive').touch()
        self.waiting=ProcessAgent([sys.executable],url=url,name='桌面状态 · 等待接入',feedback='codex')
        self.waiting.state['status']='IDLE'
        self.waiting.state['activity']={'summary':'请在所选目录提问，并在桌面应用中信任 UAH 钩子'}
        self.waiting.state['payload']['runtime'].update(source='desktop-app',soft_control=False,control_actions=[])
        self.waiting.publish()

    def close(self):
        self.stop.set()
        try:(self.channel/'listener.alive').unlink()
        except OSError:pass
        if self.lockfile:self.lockfile.close();self.lockfile=None

    def accept(self,event):
        session=event.get('session_id')
        if not session:return
        if session not in self.agents:
            runner=ProcessAgent([sys.executable],url=self.url,name='ChatGPT 桌面 · '+session[-6:],feedback='codex')
            runner.state['payload']['runtime'].update(soft_control=False,control_actions=[],source='desktop-app',
                display_mode='activity',process_running=False)
            self.agents[session]=runner
        runner=self.agents[session]
        runner.accept_feedback(event)
        runner.publish()

    def run(self):
        heartbeat=0
        while not self.stop.is_set():
            for event in self.mailbox.events():self.accept(event)
            if not self.agents and time.monotonic()-heartbeat>=3:self.waiting.publish(heartbeat=True)
            for runner in list(self.agents.values()):
                if runner.public_stages and not runner.turn_finished:
                    for event in runner.public_stages.events():
                        runner.accept_feedback(event);runner.publish()
                if time.monotonic()-heartbeat>=3:runner.publish(heartbeat=True)
            if time.monotonic()-heartbeat>=3 and not self.stop.is_set():
                (self.channel/'listener.alive').touch()
                heartbeat=time.monotonic()
            self.stop.wait(.2)


def main():
    if '--list' in sys.argv:
        print(json.dumps(discover_apps(),ensure_ascii=False));return 0
    if '--background' in sys.argv:
        pythonw=Path(sys.executable).with_name('pythonw.exe')
        executable=str(pythonw) if pythonw.exists() else sys.executable
        subprocess.Popen([executable,'-m','uah.tools.desktop_launch'],cwd=Path(__file__).resolve().parents[2],
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        return 0
    import tkinter as tk
    from tkinter import ttk,filedialog,messagebox
    root=tk.Tk();root.title('UAH · Agent 应用启动器');root.geometry('660x460')
    frame=ttk.Frame(root,padding=22);frame.pack(fill='both',expand=True)
    ttk.Label(frame,text='选择 Agent，打开应用',font=('Microsoft YaHei UI',16)).pack(anchor='w')
    try:saved=load_apps()
    except (OSError,ValueError):saved=[]
    choice=tk.StringVar(value='自动检测 ChatGPT / Codex')
    choices=ttk.Combobox(frame,textvariable=choice,state='readonly',values=['自动检测 ChatGPT / Codex']+[a['path'] for a in saved])
    choices.pack(fill='x',pady=8)
    program=tk.StringVar()
    ttk.Entry(frame,textvariable=program).pack(fill='x')
    adapter=tk.StringVar(value='Codex / ChatGPT（工作状态）')
    ttk.Combobox(frame,textvariable=adapter,state='readonly',values=['Codex / ChatGPT（工作状态）','其他应用（仅启动）']).pack(fill='x',pady=6)
    def pick_saved(event=None):
        index=choices.current()-1
        if index<0:program.set('');adapter.set('Codex / ChatGPT（工作状态）');return
        item=saved[index];program.set(item['path'])
        adapter.set('Codex / ChatGPT（工作状态）' if item['adapter']=='codex' else '其他应用（仅启动）')
    choices.bind('<<ComboboxSelected>>',pick_saved)
    def browse_app():
        value=filedialog.askopenfilename(parent=root,title='选择桌面程序，不要选择命令行版本',filetypes=[('应用或快捷方式','*.exe *.lnk')])
        if value:program.set(value);adapter.set('其他应用（仅启动）')
    ttk.Button(frame,text='添加程序路径…',command=browse_app).pack(anchor='w',pady=4)
    ttk.Label(frame,text='选择你将在应用中使用的工作目录。UAH 显示该目录的工作状态。').pack(anchor='w',pady=12)
    folder=tk.StringVar(value=os.getcwd())
    ttk.Entry(frame,textvariable=folder).pack(fill='x')
    def choose():
        value=filedialog.askdirectory(parent=root)
        if value:folder.set(value)
    ttk.Button(frame,text='选择工作目录',command=choose).pack(anchor='w',pady=8)
    status=tk.StringVar(value='路径会自动保存。Codex 工作状态需信任项目钩子；其他应用当前仅支持启动。')
    ttk.Label(frame,textvariable=status,wraplength=530).pack(anchor='w',pady=8)
    selected={}
    def launch():
        monitor=None
        try:
            selected_adapter='codex' if adapter.get().startswith('Codex') else 'launch-only'
            target=program.get().strip().strip('"')
            if target:
                saved[:]=save_app(target,selected_adapter)
                choices.configure(values=['自动检测 ChatGPT / Codex']+[a['path'] for a in saved])
                target=str(Path(target).expanduser().resolve())
            else:
                apps=discover_apps()
                if not apps:raise ValueError('未检测到桌面应用，请添加程序路径')
                target='shell:AppsFolder\\'+apps[0]['AppID']
            if selected_adapter=='launch-only':
                os.startfile(target)
                status.set('程序已打开。此应用尚未接入工作状态；路径已保存，下次可以直接选择。')
                return
            from .agent_launch import ensure_hub
            url='http://127.0.0.1:8789'
            ensure_hub(url)
            channel=install_hooks(folder.get())
            monitor=DesktopMonitor(channel,url)
            # Windows shell activation opens/focuses the installed app, not its bundled CLI.
            os.startfile(target)
            selected.update(monitor=monitor,url=url)
            root.destroy()
        except Exception as exc:
            if monitor:monitor.close()
            messagebox.showerror('启动失败',str(exc),parent=root)
    ttk.Button(frame,text='打开应用并显示 UAH',command=launch).pack(anchor='e',pady=12)
    root.mainloop()
    if not selected:return 0
    monitor=selected['monitor']
    from ..hosts.desktop.hud import HudApp
    from ..hosts.desktop.__main__ import _cleanup_and_exit
    app=HudApp(selected['url'])
    app.build();app.start_stream()
    threading.Thread(target=monitor.run,daemon=True,name='uah-desktop-feedback').start()
    try:app._root.mainloop()
    finally:
        monitor.close()
        _cleanup_and_exit(app,0)


if __name__=='__main__':main()
