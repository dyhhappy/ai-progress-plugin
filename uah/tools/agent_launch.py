"""Launch user-configured CLI agents without depending on the UHA executor."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import time
from urllib.parse import urlsplit

from ..adapters.process import ProcessAgent
from ..core.transport import HubClient, probe_hub

ROOT = Path(__file__).resolve().parents[2]


def load_profiles(path):
    path=Path(path)
    if not path.exists(): return {}
    data=json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(data,dict): raise ValueError('配置根节点必须是对象')
    for name,profile in data.items():
        if not isinstance(profile,dict) or not isinstance(profile.get('command'),list) or not profile['command']:
            raise ValueError(f'{name}: command 必须是非空字符串数组')
        if not all(isinstance(x,str) and x and '\0' not in x for x in profile['command']):
            raise ValueError(f'{name}: command 含无效参数')
        if profile.get('mode','terminal') not in ('terminal','capture'):
            raise ValueError(f'{name}: mode 必须为 terminal 或 capture')
        if profile.get('feedback','auto') not in ('auto','off','script','codex'):
            raise ValueError(f'{name}: feedback 必须为 auto、off、script 或 codex')
        if not isinstance(profile.get('share_output',False),bool):
            raise ValueError(f'{name}: share_output 必须为 true 或 false')
        if 'cwd' in profile and not isinstance(profile['cwd'],str): raise ValueError('cwd 必须是字符串')
        # Relative working directories belong to the profile file, not the caller's shell.
        if profile.get('cwd'):
            cwd=Path(os.path.expandvars(os.path.expanduser(profile['cwd'])))
            profile['cwd']=str(cwd if cwd.is_absolute() else (path.parent/cwd).resolve())
    return data


def profiles(path):
    result={}
    for executable in ('codex','claude','gemini','opencode','aider'):
        located=shutil.which(executable)
        if located: result[executable]={'command':[located],'mode':'terminal'}
    result.update(load_profiles(path))
    return result


def ensure_hub(url):
    if probe_hub(url) is not None: return
    parsed=urlsplit(url)
    if parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost') or parsed.path not in ('','/'):
        raise ValueError('自动启动仅支持本机 HTTP Hub；远程地址必须已在线')
    from .uah import _spawn_hub
    if not _spawn_hub(url): raise OSError('无法启动 Hub')
    for _ in range(50):
        if probe_hub(url) is not None: return
        time.sleep(.1)
    raise OSError('Hub 未就绪，请检查端口占用')


def main(argv=None):
    parser=argparse.ArgumentParser(description='UAH 命令行 Agent 启动器；无需模型密钥')
    parser.add_argument('--url',default='http://127.0.0.1:8789')
    parser.add_argument('--profiles',default=str(ROOT/'config/agents.local.json'))
    parser.add_argument('--profile')
    parser.add_argument('--list',action='store_true')
    parser.add_argument('--choose',action='store_true')
    parser.add_argument('--sample',choices=('plain','events','failure','input'))
    parser.add_argument('--name',default=None)
    parser.add_argument('--cwd')
    parser.add_argument('--mode',choices=('terminal','capture'))
    parser.add_argument('--encoding',default='utf-8')
    parser.add_argument('--feedback',choices=('auto','off','script','codex'),default=None)
    parser.add_argument('--share-output',action='store_true',help='将清理后的输出摘要发给本机 HUD；默认只显示行数')
    parser.add_argument('--hud',action='store_true',help='同时新开一个 HUD 窗口')
    parser.add_argument('command',nargs=argparse.REMAINDER,help='-- 后填写程序及参数，不是 shell 表达式')
    args=parser.parse_args(argv)
    interactive=args.choose or not (args.command or args.profile or args.sample or args.list)
    try:
        available=profiles(args.profiles)
        if args.list:
            for name,p in available.items(): print(f'{name}: {p.get("mode","terminal")} | {p["command"][0]}')
            if not available: print('没有检测到 CLI；可在 config/agents.local.json 配置，或用 -- 后指定程序。')
            return 0
        if interactive:
            print('\n╔══════════════════════════════════════════════╗\n║       UAH · Agent 应用启动器                  ║\n╚══════════════════════════════════════════════╝\n自动检测常用命令行 Agent；选择后直接打开。\n真实 AI 需先完成安装和登录。\n')
            names=list(available)
            for i,name in enumerate(names,1):
                p=available[name]
                print(f'  [{i}] {p.get("name",name)}\n      {p["command"][0]}')
            missing=[n for n in ('codex','claude','gemini','opencode','aider') if n not in available]
            if missing: print('\n未检测到：'+', '.join(missing)+'（不会自动安装）')
            print('s. 子进程链路测试（不是 AI）\n0. 退出')
            choice=input('选择：').strip()
            if choice=='0': return 0
            if choice.lower()=='s': args.sample='events'
            elif choice.isdigit() and 1<=int(choice)<=len(names): args.profile=names[int(choice)-1]
            else: raise ValueError('无效选择')
        config={}
        if args.sample:
            config={'command':[sys.executable,'-u',str(ROOT/'uah/tools/process_sample.py'),args.sample],
                    'mode':'capture','name':'子进程链路测试（非 AI）'}
        elif args.profile:
            if args.profile not in available: raise ValueError('找不到指定配置：'+args.profile)
            config=available[args.profile]
            if interactive and not args.cwd and not config.get('cwd'):
                chosen=input(f'工作目录（回车使用 {os.getcwd()}）：').strip()
                args.cwd=chosen or os.getcwd()
        command=args.command
        if command and command[0]=='--': command=command[1:]
        if command and (args.profile or args.sample): raise ValueError('直接命令、配置和样例只能选择一种')
        command=command or config.get('command')
        if not command: raise ValueError('请用 -- 后指定命令，或者使用 --profile')
        feedback=args.feedback or config.get('feedback','auto')
        if feedback=='auto':
            feedback='codex' if Path(command[0]).stem.lower()=='codex' else 'script'
            if feedback=='codex' and Path(command[0]).suffix.lower() in ('.cmd','.bat'):
                feedback='script'
                print('[UAH] 当前是批处理入口，仅启用脚本反馈；要自动识别 Codex 回合，请配置 codex.exe 的绝对路径。')
        ensure_hub(args.url)
        if args.hud:
            from ..hosts.desktop.launch import spawn_hud
            if not spawn_hud(args.url): print('HUD 未启动，但终端任务仍可运行。',file=sys.stderr)
        wrapper=ProcessAgent(command,url=args.url,cwd=args.cwd or config.get('cwd'),
            name=args.name or config.get('name') or args.profile or Path(command[0]).stem,
            mode=args.mode or config.get('mode','terminal'),
            share_output=args.share_output or config.get('share_output',False),encoding=args.encoding,
            feedback=feedback)
        print(f'[UAH] Agent ID: {wrapper.agent_id}，模式：{wrapper.mode}。\n'
              '请在 HUD 的“当前 Agent”菜单选择它。停止将结束本次进程，不会回滚修改。',flush=True)
        return wrapper.run()
    except (ValueError,OSError,EOFError) as exc:
        print(f'[UAH] {exc}',file=sys.stderr)
        return 2


if __name__=='__main__': raise SystemExit(main())
