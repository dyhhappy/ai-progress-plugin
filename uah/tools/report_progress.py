"""One-shot AI progress reporter and Codex lifecycle hook. Emits no blocking decisions."""
import argparse
import json
import os
import re
import time
from pathlib import Path
import sys

if __package__ in (None,''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from uah.adapters.feedback import write_event


def read_hook_input(stream):
    # Windows shell pipelines can prepend a UTF-8 BOM. json.loads(str)
    # rejects that marker even though the rest is valid JSON.
    text=stream.read(8*1024*1024+1)
    if len(text)>8*1024*1024: raise ValueError('hook input exceeds 8 MiB')
    text=text.lstrip('\ufeff \r\n\t')
    if not text: raise ValueError('empty hook input')
    raw=json.loads(text)
    if not isinstance(raw,dict): raise ValueError('hook input must be an object')
    return raw


def tool_activity(tool, data, finished=False):
    # Describe observed operations, without exposing queries, paths or command bodies.
    key=re.sub(r'[^a-z0-9]', '', tool.lower())
    if key.endswith(('webrun','websearch','websearchpreview')):
        if any(data.get(k) for k in ('search_query','image_query')): action='搜索资料'
        elif any(data.get(k) for k in ('open','click','find','screenshot')): action='阅读网页资料'
        else: action='查询网络资料'
    elif key.endswith(('applypatch','writefile','editfile')): action='修改文件'
    elif key.endswith(('readfile','viewimage')): action='查看文件'
    elif key.endswith(('execcommand','bash','powershell')): action='执行终端操作'
    elif key.endswith('updateplan'): action='更新任务计划'
    elif key.endswith(('imagegen','generateimage')): action='生成图片'
    else: action='调用工具'
    return action+'已结束' if finished else '正在'+action


def hook_event(raw):
    if not isinstance(raw,dict) or raw.get('agent_id') or raw.get('subagent_id'): return None
    name=raw.get('hook_event_name')
    kind={'SessionStart':'ready','UserPromptSubmit':'turn_started','Stop':'turn_finished',
          'Interrupt':'turn_interrupted','PermissionRequest':'waiting_approval',
          'PreToolUse':'activity','PostToolUse':'activity'}.get(name)
    if not kind: return None
    event={'type':kind,'turn_id':str(raw.get('turn_id') or ''),'source':'codex-hook'}
    event['session_id']=str(raw.get('session_id') or '')
    if name=='UserPromptSubmit' and isinstance(raw.get('transcript_path'),str):
        event['transcript_path']=raw['transcript_path']
    if name in ('PreToolUse','PostToolUse'):
        tool=str(raw.get('tool_name') or '')
        label={'Bash':'终端命令','exec_command':'终端命令','apply_patch':'文件修改',
               'update_plan':'任务计划'}.get(tool,tool[:100] or '工具')
        event['message']=('正在执行：' if name=='PreToolUse' else '执行结束：')+label
        data=raw.get('tool_input') or {}
        if isinstance(data,str):
            try:data=json.loads(data)
            except ValueError:data={}
        if not isinstance(data,dict):data={}
        event['tool_activity']=tool_activity(tool,data,name=='PostToolUse')
        event['message']=event['tool_activity']
        if isinstance(data,dict) and 'report_progress.py' in str(data.get('command') or data.get('cmd') or ''):
            return None  # Preserve the stage being reported, not the reporter's own shell activity.
        if name=='PostToolUse' and tool=='update_plan' and isinstance(data,dict) and isinstance(data.get('plan'),list):
            plan=data['plan']
            if plan and len(plan)<=1000 and all(isinstance(p,dict) and p.get('status') in ('pending','in_progress','completed') for p in plan):
                event.update(type='plan',completed=sum(p['status']=='completed' for p in plan),total=len(plan))
                active=next((p for p in plan if p['status']=='in_progress'),None)
                event['message']='当前阶段：'+str(active.get('step','正在执行计划'))[:180] if active else '任务计划已更新'
    return event


def instructions(channel, turn_id='', session_id=''):
    script=Path(__file__).resolve()
    turn_id=turn_id if re.fullmatch(r'[A-Za-z0-9_.:-]{0,160}',turn_id) else ''
    quote=lambda value: "'"+str(value).replace("'","''")+"'"
    if os.name=='nt':
        command=f'& {quote(sys.executable)} {quote(script)} --channel {quote(channel)} --turn-id {quote(turn_id)}'
    else:
        import shlex
        command=shlex.join([sys.executable,str(script),'--channel',str(channel),'--turn-id',turn_id])
    if session_id and re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}',session_id):
        command+=' --session-id '+session_id
    return ('UAH 当前显示简短工作状态，不需要百分比。每轮对话无需用户提醒：在实质阶段变化时，'
            '优先发送一条对用户公开的 commentary，格式为“当前阶段：正在搜集资料”或“当前阶段：正在制作 PPT”。'
            '按实际任务命名，也可以是“正在检查排版”“正在整理回答”，不要照抄不相关的示例。'
            'UAH 会读取本轮公开 commentary；不要只在最终答案里写过程。也可以'
            '用下面命令上报一句简短、面向用户的工作摘要，把示例文字替换成实际阶段：\n'
            f'{command} --stage "正在整理回答"\n'
            '例如确定回答范围、整理资料、核对结果。仅报告实际发生的工作，不编造工具动作，'
            '不输出内部思维链、隐藏推理或敏感内容。工具开始和结束由钩子自动记录。'
            '普通问答可在组织答案时上报一次；短回答无需人为延长。'
            '不要再计算或上报百分比，也不要自行发送 --start 或 --done。'
            '反馈失败继续原任务，不为反馈申请额外权限。')



def main(argv=None):
    parser=argparse.ArgumentParser(description='向本次 UAH 会话上报 AI 估计进度')
    parser.add_argument('--channel',default=os.environ.get('UAH_REPORT_CHANNEL'))
    parser.add_argument('--turn-id',default='')
    parser.add_argument('--session-id',default='')
    parser.add_argument('--percent',type=float)
    parser.add_argument('--completed',type=int)
    parser.add_argument('--total',type=int)
    parser.add_argument('--stage',default='')
    parser.add_argument('--hook',action='store_true')
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--start',action='store_true')
    group.add_argument('--done',action='store_true')
    group.add_argument('--failed',action='store_true')
    args=parser.parse_args(argv)
    try:
        if not args.channel:
            if args.hook:
                print(json.dumps({'systemMessage':'UAH 反馈通道未传入，请通过 UAH 启动器重新启动本次 Agent。'},ensure_ascii=False))
                return 0
            raise ValueError('未找到 UAH_REPORT_CHANNEL，请在 UAH 启动的 Agent 中调用')
        if Path(args.channel).name=='uah-feedback-desktop':
            try:active=time.time()-(Path(args.channel)/'listener.alive').stat().st_mtime<15
            except OSError:active=False
            if not active:
                if args.hook:print('{}');return 0
                raise ValueError('桌面 UAH 窗口已关闭，请重新打开启动器')
        if args.hook:
            raw=read_hook_input(sys.stdin)
            event=hook_event(raw)
            if event: write_event(args.channel,event)
            if event and raw.get('hook_event_name') in ('SessionStart','UserPromptSubmit'):
                print(json.dumps({'hookSpecificOutput':{'hookEventName':raw['hook_event_name'],
                    'additionalContext':instructions(args.channel,str(raw.get('turn_id') or ''),str(raw.get('session_id') or ''))}},ensure_ascii=False))
            else:
                print('{}')
        else:
            if args.percent is not None and not 0<=args.percent<=99:
                raise ValueError('进行中的估计百分比必须在 0–99 之间，结束请用 --done')
            has_plan=args.completed is not None or args.total is not None
            if has_plan and (args.completed is None or args.total is None or not 0<=args.completed<=args.total or args.total<=0):
                raise ValueError('步骤上报需要 --completed 与 --total，且 0 ≤ 已完成 ≤ 总量，总量 > 0')
            if has_plan and (args.percent is not None or args.start or args.done or args.failed):
                raise ValueError('步骤、估计百分比和回合状态请分别上报')
            if not (args.start or args.done or args.failed or args.percent is not None or args.stage or has_plan):
                raise ValueError('请提供阶段、估计百分比或回合状态')
            kind='turn_started' if args.start else 'turn_finished' if args.done else 'turn_failed' if args.failed else 'plan' if has_plan else 'estimate'
            write_event(args.channel,{'type':kind,'turn_id':args.turn_id,'percent':args.percent,
                                      'session_id':args.session_id,
                                      'completed':args.completed,'total':args.total,
                                      'message':args.stage[:240],'source':'ai-report'})
        return 0
    except (OSError,ValueError,TypeError,RecursionError) as exc:
        # Hooks are advisory: no stderr output or nonzero status that could block the Agent.
        if args.hook:
            try:
                write_event(args.channel,{'type':'feedback_error','message':'反馈输入解析失败：'+type(exc).__name__})
            except (OSError,ValueError,TypeError):pass
            print(json.dumps({'systemMessage':'UAH 进度反馈失败：'+type(exc).__name__+'；请检查反馈脚本和通道。'},ensure_ascii=False))
            return 0
        print(f'[UAH] {exc}',file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
