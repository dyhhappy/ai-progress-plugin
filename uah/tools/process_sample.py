"""A real subprocess fixture, explicitly NOT an AI agent."""
import json
import sys
import time

mode=sys.argv[1] if len(sys.argv)>1 else 'plain'
delay=float(sys.argv[2]) if len(sys.argv)>2 else 2
def event(**data): print('UAH_EVENT '+json.dumps(data,ensure_ascii=False),flush=True)
print('子进程链路样例：不是 AI，不操作 UE。',flush=True)
if mode=='input':
    event(type='waiting_input',message='请在启动器终端输入文字并回车')
    input('输入：')
    event(type='running',message='已收到终端输入')
for i in range(3):
    if mode!='plain':event(type='progress',completed=i,total=3,message=f'样例步骤 {i+1}')
    else: print(f'普通输出 {i+1}',flush=True)
    time.sleep(delay)
    if mode=='failure' and i==1:
        event(type='failed',message='样例主动失败');sys.exit(7)
if mode!='plain':
    event(type='progress',completed=3,total=3)
    event(type='completed')
