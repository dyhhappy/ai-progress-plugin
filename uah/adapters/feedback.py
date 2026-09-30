"""Per-launch file mailbox. Never reads unrelated conversations or installs global hooks."""
import json
import os
from pathlib import Path
import tempfile
import time
import uuid


class FeedbackChannel:
    def __init__(self):
        self._temp = tempfile.TemporaryDirectory(prefix='uah-feedback-')
        self.path = Path(self._temp.name)

    def events(self):
        for path in sorted(self.path.glob('*.json'))[:128]:
            try:
                if path.stat().st_size > 16384:
                    continue
                event = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(event, dict): yield event
            except (OSError,ValueError):
                pass
            finally:
                try: path.unlink()
                except OSError: pass

    def close(self):
        try: self._temp.cleanup()
        except OSError: pass


def write_event(channel, event):
    path=Path(channel)
    if not path.is_dir() or not path.name.startswith('uah-feedback-'):
        raise ValueError('反馈通道不存在；请通过 UAH 启动 Agent')
    if len(list(path.glob('*.json'))) >= 256:
        raise ValueError('反馈过快，通道已满')
    payload=json.dumps(event,ensure_ascii=False).encode('utf-8')
    if len(payload)>16384: raise ValueError('反馈内容过长')
    name=f'{time.time_ns():020d}-{uuid.uuid4().hex}'
    temporary=path/(name+'.tmp')
    temporary.write_bytes(payload)
    os.replace(temporary,path/(name+'.json'))


def codex_hook_arguments(channel=None):
    import sys
    import base64
    executable=Path(sys.executable)
    if executable.name.lower()=='pythonw.exe':
        executable=executable.with_name('python.exe')
        if not executable.is_file():raise OSError('钩子需要同目录的 python.exe 才能读取输入和返回反馈')
    script=Path(__file__).resolve().parents[1]/'tools/report_progress.py'
    # Literal paths, no user-controlled prompt interpolation. Stable command means stable hook review.
    command=f'"{executable}" "{script}" --hook'
    if channel:
        import shlex
        command += ' --channel '+shlex.quote(str(channel))
    if os.name=='nt':
        quote=lambda value: "'"+str(value).replace("'","''")+"'"
        # Hooks may start under a different shell. Explicit PowerShell makes the
        # call operator and Unicode stdin independent of that parent shell.
        source=("$ProgressPreference='SilentlyContinue';"
                "$OutputEncoding=[Console]::InputEncoding=[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new();"
                "$env:PYTHONUTF8='1';$uahInput=[Console]::In.ReadToEnd();"
                f'$uahInput | & {quote(executable)} {quote(script)} --hook'
                +(f' --channel {quote(channel)}' if channel else '')+';exit $LASTEXITCODE')
        encoded=base64.b64encode(source.encode('utf-16-le')).decode('ascii')
        command='powershell.exe -NoProfile -NonInteractive -EncodedCommand '+encoded
    args=[]
    for name in ('SessionStart','UserPromptSubmit','PostToolUse','PermissionRequest','Stop','Interrupt','PreToolUse'):
        value='[{hooks=[{type="command",command='+json.dumps(command)+',timeout=3}]}]'
        args += ['-c',f'hooks.{name}={value}']
    return args
