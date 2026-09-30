"""Own one user-selected CLI process; never infer task success from exit code alone."""
from __future__ import annotations

import codecs
import copy
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from ..core.protocol import PROTOCOL_VERSION
from ..core.transport import HubClient

PREFIX = 'UAH_EVENT '
MAX_LINE = 16384
ANSI = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))')


def clean_text(value, limit=240):
    text = ANSI.sub('', str(value))
    text = ''.join(c for c in text if c >= ' ' and c != '\x7f')
    text = re.sub(r'(?i)\b(bearer\s+)\S+', r'\1[redacted]', text)
    text = re.sub(r'(?i)((?:api[_-]?key|token|password|secret)\s*[:=]\s*)\S+', r'\1[redacted]', text)
    text = re.sub(r'\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{12,}', '[redacted]', text)
    return text[:limit]


def resolve_command(command, cwd):
    if not command or any(not isinstance(x, str) or '\0' in x for x in command):
        raise ValueError('命令必须是非空字符串数组')
    first = os.path.expandvars(os.path.expanduser(command[0]))
    candidate = Path(first)
    if not candidate.is_absolute():
        candidate = Path(cwd) / candidate
    executable = str(candidate.resolve()) if candidate.is_file() else shutil.which(first)
    if not executable:
        raise FileNotFoundError(f'找不到程序：{first}。请先安装或配置绝对路径。')
    args = [executable, *command[1:]]
    if Path(executable).suffix.lower() == '.py':
        return [sys.executable, '-u', *args]
    if os.name == 'nt' and Path(executable).suffix.lower() in ('.cmd', '.bat'):
        # cmd.exe has a second parser; reject shell metacharacters rather than execute them.
        if any(re.search(r'["&|<>^%!\r\n]', arg) for arg in args):
            raise ValueError('批处理参数含 shell 特殊字符；请在配置中直接使用 node.exe 和 CLI 的 JS 入口。')
        shell = os.environ.get('COMSPEC', 'C:/Windows/System32/cmd.exe')
        return '"' + shell + '" /d /s /c "' + ' '.join('"' + arg + '"' for arg in args) + '"'
    return args


class ProcessGroup:
    """Windows Job Object / POSIX session: termination affects only this launch."""
    def __init__(self):
        self.handle = None
        self.proc = None
        if os.name != 'nt':
            return
        import ctypes as c
        from ctypes import wintypes as w
        class IO(c.Structure):
            _fields_ = [(n, c.c_ulonglong) for n in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
        class Basic(c.Structure):
            _fields_ = [('process_time',c.c_longlong),('job_time',c.c_longlong),
                        ('flags',w.DWORD),('min_ws',c.c_size_t),('max_ws',c.c_size_t),
                        ('active',w.DWORD),('affinity',c.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
        class Extended(c.Structure):
            _fields_ = [('basic',Basic),('io',IO),('process_mem',c.c_size_t),('job_mem',c.c_size_t),
                        ('peak_process',c.c_size_t),('peak_job',c.c_size_t)]
        self.k = c.WinDLL('kernel32', use_last_error=True)
        self.k.CreateJobObjectW.argtypes = [c.c_void_p,w.LPCWSTR]
        self.k.CreateJobObjectW.restype = w.HANDLE
        self.k.SetInformationJobObject.argtypes = [w.HANDLE,c.c_int,c.c_void_p,w.DWORD]
        self.k.AssignProcessToJobObject.argtypes = [w.HANDLE,w.HANDLE]
        self.k.TerminateJobObject.argtypes = [w.HANDLE,w.UINT]
        self.k.CloseHandle.argtypes = [w.HANDLE]
        self.handle = self.k.CreateJobObjectW(None,None)
        if not self.handle:
            raise c.WinError(c.get_last_error())
        limits = Extended(); limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.k.SetInformationJobObject(self.handle,9,c.byref(limits),c.sizeof(limits)):
            error = c.WinError(c.get_last_error()); self.close(); raise error

    def attach(self, proc):
        self.proc = proc
        if self.handle and not self.k.AssignProcessToJobObject(self.handle,int(proc._handle)):
            if proc.poll() is None:
                import ctypes
                raise ctypes.WinError(ctypes.get_last_error())

    def terminate(self, proc):
        if self.handle:
            if not self.k.TerminateJobObject(self.handle,130):
                import ctypes
                raise ctypes.WinError(ctypes.get_last_error())
        elif proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            if os.name != 'nt': os.killpg(proc.pid,signal.SIGKILL)
            else: proc.kill()
            proc.wait(timeout=2)

    def close(self):
        if os.name != 'nt' and self.proc is not None:
            try: os.killpg(self.proc.pid,signal.SIGKILL)
            except ProcessLookupError: pass
            self.proc = None
        if self.handle:
            self.k.CloseHandle(self.handle); self.handle = None


class ProcessAgent:
    def __init__(self, command, *, url='http://127.0.0.1:8789', cwd=None,
                 name='命令行 Agent', mode='terminal', share_output=False, encoding='utf-8', feedback='script'):
        if mode not in ('terminal','capture'):
            raise ValueError('mode 必须为 terminal 或 capture')
        codecs.lookup(encoding)
        if feedback not in ('off','script','codex'): raise ValueError('无效反馈模式')
        self.feedback_mode = feedback
        self.feedback_channel = None
        self.public_stages = None
        self.public_stage = None
        self.turn_id = None
        self.turn_finished = False
        self.seen_turns = set()
        self.process_lock = threading.RLock()
        self.command = list(command)
        self.cwd = str(Path(cwd or os.getcwd()).resolve())
        if not Path(self.cwd).is_dir(): raise ValueError('工作目录不存在')
        self.url,self.mode,self.share_output,self.encoding = url,mode,share_output,encoding
        self.agent_id = 'cli-' + uuid.uuid4().hex[:12]
        self.task_id = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.publish_lock = threading.Lock()
        self.finished = threading.Event()
        self.changed = threading.Event()
        self.proc = None
        self.group = None
        self.cancelled = False
        self.reported_success = False
        self.reported_error = False
        self.lines = 0
        self.state = {'status':'STARTING','task':{'id':self.task_id,'name':clean_text(name),
                     'started_at':time.time(),'total_steps':None,'completed_steps':None},
                     'activity':{'summary':'正在启动命令行程序'},
                     'payload':{'runtime':{'task_domain':Path(self.cwd).name,'soft_control':True,
                         'control_actions':['stop'],'source':'cli-wrapper','progress_basis':'reported',
                         'process_running':False,'estimated_percent':None,'feedback':feedback,
                         'display_mode':'activity' if feedback=='codex' else 'progress',
                         'approval':'未接入','stop_semantics':'结束进程，不代表回滚', 'capture_mode':mode}}}
        self.agent = {'id':self.agent_id,'name':clean_text(name),'type':'cli'}

    def event(self, heartbeat=False):
        with self.lock:
            state = copy.deepcopy(self.state)
        return {'protocol':PROTOCOL_VERSION,'event_id':uuid.uuid4().hex,
                'timestamp':time.time(),'type':'agent.heartbeat' if heartbeat else 'status.changed',
                'agent':self.agent,**state}

    def publish(self, heartbeat=False):
        try:
            with self.publish_lock:
                HubClient(self.url,timeout_s=.6).post_event(self.event(heartbeat))
            return True
        except Exception:
            return False

    def _sender(self):
        while not self.finished.is_set():
            changed = self.changed.wait(1)
            self.changed.clear()
            self.publish(heartbeat=not changed)

    def accept_line(self, line):
        with self.lock:
            self.lines += 1
            if line.startswith(PREFIX):
                try:
                    event = json.loads(line[len(PREFIX):])
                    self._structured(event)
                except (ValueError,TypeError,KeyError,RecursionError):
                    self.state['activity'] = {'summary':'收到无效结构化事件，已忽略'}
            elif not self.reported_error and not self.reported_success:
                self.state['activity'] = {'summary':clean_text(line) if self.share_output else
                    f'程序有输出（{self.lines} 行，原文仅保留在终端）'}
            self.changed.set()

    def _structured(self, event):
        if not isinstance(event,dict): raise ValueError('object required')
        kind = event.get('type')
        if kind not in ('activity','progress','waiting_input','waiting_approval','running','completed','failed'):
            raise ValueError('unsupported type')
        if self.reported_error or (self.reported_success and kind != 'failed'): return
        completed,total = event.get('completed'),event.get('total')
        if kind == 'progress':
            if type(completed) is not int or type(total) is not int or not 0 <= completed <= total or total <= 0:
                raise ValueError('invalid progress')
            self.state['task'].update(completed_steps=completed,total_steps=total)
        if kind == 'completed':
            self.reported_success = True
            # Keep RUNNING until the owned process has really exited successfully.
            self.state['status'] = 'RUNNING'
            self.state['activity'] = {'summary':'程序报告完成，等待进程退出'}
        elif kind == 'failed':
            self.reported_error = True
            self.state['status'] = 'BLOCKED'
            self.state['activity'] = {'summary':'程序报告失败，等待进程结束'}
        else:
            self.state['status'] = {'waiting_input':'WAITING_INPUT',
                'waiting_approval':'WAITING_APPROVAL'}.get(kind,'RUNNING')
            self.state['activity'] = {'summary':clean_text(event.get('message') or {
                'waiting_input':'等待终端输入','waiting_approval':'等待终端确认',
                'progress':'程序上报步骤进度'}.get(kind,'程序正在运行'))}

    def _reader(self, pipe, parse):
        # Incremental decoding and bounded records: binary/no-newline output cannot grow memory.
        decoder = codecs.getincrementaldecoder(self.encoding)(errors='replace')
        pending = ''
        try:
            while True:
                chunk = os.read(pipe.fileno(),4096)
                if not chunk: break
                text = decoder.decode(chunk)
                stream = sys.stdout if parse else sys.stderr
                try: stream.write(text); stream.flush()
                except (OSError,UnicodeError): pass
                pending += text.replace('\r','\n')
                while '\n' in pending or len(pending) > MAX_LINE:
                    cut = pending.find('\n')
                    if cut < 0 or cut > MAX_LINE:
                        cut = MAX_LINE
                        record,pending = pending[:cut],pending[cut:]
                        # Oversize fragments cannot be protocol messages.
                        record = '[long output] ' + record
                    else: record,pending = pending[:cut],pending[cut+1:]
                    if record:
                        self.accept_line(record if parse else '[stderr] '+record)
            pending += decoder.decode(b'',final=True)
            if pending: self.accept_line(pending if parse else '[stderr] '+pending)
        except (OSError,ValueError):
            pass
        finally:
            pipe.close()

    def accept_feedback(self, event):
        kind = event.get('type')
        incoming = str(event.get('turn_id') or '')
        with self.lock:
            if self.cancelled or self.finished.is_set(): return
            meta = self.state['payload']['runtime']
            if kind == 'feedback_error':
                meta['feedback_error']=clean_text(event.get('message','反馈失败'))
                self.changed.set()
                return
            meta.pop('feedback_error',None)
            if kind == 'ready' and self.turn_id is None:
                self.state['status']='IDLE'
                self.state['activity']={'summary':'已就绪，等待终端提问'}
            elif kind == 'turn_started':
                if incoming and incoming in self.seen_turns: return
                self.turn_id=incoming or uuid.uuid4().hex
                self.public_stage=None
                if self.feedback_mode=='codex' and event.get('source')=='codex-hook' and event.get('transcript_path'):
                    from .public_stages import PublicStages
                    self.public_stages=PublicStages(event['transcript_path'],self.turn_id)
                else:self.public_stages=None
                self.seen_turns.add(self.turn_id)
                self.turn_finished=False
                self.state['task']={'id':self.task_id+':'+self.turn_id,'name':self.agent['name']+' · 当前问答',
                    'started_at':time.time(),'ended_at':None,'total_steps':None,'completed_steps':None}
                self.state['status']='RUNNING'
                self.state['activity']={'summary':'正在处理本轮问题'}
                meta['estimated_percent']=None
            elif self.turn_id is not None and incoming and incoming != self.turn_id:
                return  # Late feedback from an older turn must not finish the current one.
            elif kind in ('turn_finished','turn_interrupted','turn_failed'):
                self.turn_finished=True
                self.state['status']={'turn_finished':'DONE','turn_interrupted':'CANCELLED','turn_failed':'ERROR'}[kind]
                self.state['task']['ended_at']=time.time()
                self.state['activity']={'summary':{'turn_finished':'本轮回答已结束，等待下一个问题',
                    'turn_interrupted':'本轮已中断，终端仍可继续使用','turn_failed':'本轮报告失败，终端仍可继续使用'}[kind]}
            elif self.turn_finished:
                return
            elif kind in ('estimate','plan','activity','waiting_approval'):
                if kind=='estimate':
                    value=event.get('percent')
                    if value is not None:
                        if isinstance(value,bool) or not isinstance(value,(int,float)) or not 0<=value<=99:return
                        meta['estimated_percent']=value
                elif kind=='plan':
                    completed,total=event.get('completed'),event.get('total')
                    if type(completed) is not int or type(total) is not int or not 0<=completed<=total or total<=0:return
                    self.state['task'].update(completed_steps=completed,total_steps=total)
                self.state['status']='WAITING_APPROVAL' if kind=='waiting_approval' else 'RUNNING'
                message=event.get('message') or ('请在终端处理确认' if kind=='waiting_approval' else '任务阶段已更新')
                tool_action=event.get('tool_activity')
                if kind=='activity' and tool_action:
                    self.state['activity']={'summary':self.public_stage or clean_text(message),
                                            'detail':clean_text(tool_action)}
                else:
                    if kind in ('activity','estimate','plan') and event.get('source') in ('public-commentary','ai-report'):
                        self.public_stage=clean_text(message)
                    self.state['activity']={'summary':clean_text(message)}
            else:return
            meta['feedback_connected']=True
            self.changed.set()

    def _feedback(self):
        while not self.finished.is_set():
            for event in self.feedback_channel.events(): self.accept_feedback(event)
            if self.public_stages and not self.turn_finished:
                for event in self.public_stages.events():self.accept_feedback(event)
            self.finished.wait(.1)

    def stop_process(self):
        # Never hold the state/publisher lock while waiting for the OS.
        with self.process_lock:
            if self.cancelled:return True,'进程已经停止'
            if self.proc is None or self.proc.poll() is not None:
                return False,'进程已经退出'
            with self.lock:
                self.state['activity']={'summary':'正在终止本次进程…'}
                self.changed.set()
            self.group.terminate(self.proc)
            with self.lock:
                self.cancelled=True
                self.state['status']='CANCELLED'
                self.state['task']['ended_at']=time.time()
                self.state['activity']={'summary':'本次进程已停止'}
                self.state['payload']['runtime'].update(process_running=False,soft_control=False,control_actions=[])
                self.changed.set()
        return True,'本次启动的进程已结束；这不代表撤销其已执行的操作'

    def _controls(self):
        from ..core.approval import fetch_control_commands, acknowledge_control
        while not self.finished.is_set():
            try:
                commands = fetch_control_commands(self.url,wait_s=.1,timeout_s=1,agent_id=self.agent_id)
            except Exception as exc:
                with self.lock:
                    self.state['payload']['runtime']['control_error']=clean_text(type(exc).__name__)
                self.finished.wait(.15)
                continue
            for command in commands:
                ok,message=False,'此接入仅支持停止，不支持暂停或继续'
                try:
                    if command.get('agent') != self.agent_id or time.time() >= command.get('expires_at',0):
                        message='请求目标不匹配或已过期'
                    elif command.get('action')=='stop':ok,message=self.stop_process()
                except Exception as exc:
                    message='终止失败：'+clean_text(exc,140)
                    with self.lock:
                        self.state['payload']['runtime']['control_error']=message
                        self.state['activity']={'summary':message}
                        self.changed.set()
                for attempt in range(2):
                    try:
                        acknowledge_control(self.url,command,ok,message);break
                    except Exception:
                        if attempt==0:time.sleep(.1)
                # Publish immediately after the receipt, not after thread cleanup.
                self.publish()

    def run(self):
        readers=[]; workers=[]; code=127
        self.publish()
        try:
            command=list(self.command)
            env=os.environ.copy()
            if self.feedback_mode!='off':
                from .feedback import FeedbackChannel, codex_hook_arguments
                self.feedback_channel=FeedbackChannel()
                env['UAH_REPORT_CHANNEL']=str(self.feedback_channel.path)
                if self.feedback_mode=='codex':
                    command[1:1]=codex_hook_arguments()
                    print('[UAH] 首次使用请在 Codex 输入 /hooks，审核并信任 UAH 的回合反馈钩子。',flush=True)
            args=resolve_command(command,self.cwd)
            self.group=ProcessGroup()
            self.proc=subprocess.Popen(args,cwd=self.cwd,stdin=None,env=env,
                stdout=subprocess.PIPE if self.mode=='capture' else None,
                stderr=subprocess.PIPE if self.mode=='capture' else None,
                creationflags=getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0),
                start_new_session=os.name!='nt')
            self.group.attach(self.proc)
            with self.lock:
                self.state['payload']['runtime']['process_running']=True
                self.state['status']='RUNNING'
                self.state['activity']={'summary':'进程运行中；请在终端操作' if self.mode=='terminal' else '正在接收程序输出'}
            targets=[self._sender,self._controls]
            if self.feedback_channel:targets.append(self._feedback)
            for target in targets:
                thread=threading.Thread(target=target,daemon=True);thread.start();workers.append(thread)
            if self.mode=='capture':
                for pipe,parse in ((self.proc.stdout,True),(self.proc.stderr,False)):
                    thread=threading.Thread(target=self._reader,args=(pipe,parse),daemon=True)
                    thread.start();readers.append(thread)
            self.changed.set()
            code=self.proc.wait()
        except KeyboardInterrupt:
            if self.proc is not None: self.stop_process()
            code=130
        except (OSError,ValueError) as exc:
            with self.lock: self.state['activity']={'summary':clean_text(exc)}
            if self.proc is not None and self.proc.poll() is None:
                self.proc.kill();self.proc.wait(timeout=3)
        finally:
            with self.process_lock:
                if self.group: self.group.close()
            for thread in readers: thread.join(timeout=2)
            if self.feedback_channel:
                for event in self.feedback_channel.events(): self.accept_feedback(event)
            self.finished.set()
            with self.lock:
                complete=self.reported_success and not self.reported_error and code==0
                self.state['status']=('CANCELLED' if self.cancelled else 'ERROR' if code!=0 or self.reported_error
                                      else 'DONE' if complete or self.turn_finished and self.state['status']=='DONE' else 'WARNING')
                self.state['task']['ended_at']=time.time()
                self.state['payload']['runtime']['process_running']=False
                self.state['payload']['runtime']['soft_control']=False
                self.state['payload']['runtime']['control_actions']=[]
                self.state['payload']['runtime']['exit_code']=code
                if self.proc is not None:
                    self.state['activity']={'summary':'进程已停止' if self.cancelled else
                        '进程退出异常或程序报告失败' if code!=0 or self.reported_error else
                        '程序自报完成，进程正常退出（未经独立验证）' if complete else
                        '进程正常退出；任务结果未验证'}
            delivered=False
            for _ in range(3):
                if self.publish(): delivered=True;break
            for thread in workers: thread.join(timeout=1)
            if self.feedback_channel:self.feedback_channel.close()
            print(f'\n[UAH] {self.agent_id}: {self.state["activity"]["summary"]}；退出码 {code}',flush=True)
            if not delivered: print('[UAH] Hub 不可达，最终状态未送达。',file=sys.stderr)
        return 130 if self.cancelled else code
