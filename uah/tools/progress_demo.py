"""Publish clearly labelled demo tasks. Never connects to Unreal or injects input."""
import argparse
import os
import time
from ..adapters.uha.adapter import UhaNativeAdapter
from ..adapters.publisher import HttpPublisher
from ..core.control_poller import ControlPoller
from ..core.transport import HubClient
from src.core.session_control import SessionController, TaskPhase


def run_demo(adapter, controller, scenario, step_seconds):
    controller.acknowledge_control()
    if scenario=='unknown':
        adapter.note_single_run('演示：未知总量')
    else:
        adapter.plan_begin('演示：'+{'success':'三步完成','failure':'第二步失败','pause':'暂停与继续'}[scenario],3)
    count=1 if scenario=='unknown' else 3
    for index in range(1,count+1):
        if scenario!='unknown':adapter.plan_step(index,f'演示步骤 {index}')
        controller.begin_task(f'演示步骤 {index}')
        controller.set_phase(TaskPhase.STRUCTURED_EXECUTION)
        if scenario=='pause' and index==2:
            controller.pause()
            print('第二步已暂停，请在 HUD 点击“继续”或“停止”。',flush=True)
        deadline=time.monotonic()+step_seconds
        while time.monotonic()<deadline or controller.snapshot().command.value=='PAUSE':
            if controller.should_abort():
                if scenario=='unknown':adapter.single_end(False,False)
                else:adapter.plan_end(False)
                return
            if controller.snapshot().command.value=='PAUSE':
                time.sleep(.05);deadline+=.05
            else:time.sleep(.05)
        if controller.should_abort():
            if scenario=='unknown':adapter.single_end(False,False)
            else:adapter.plan_end(False)
            return
        controller.set_phase(TaskPhase.VERIFYING)
        time.sleep(min(step_seconds,.7))
        ok=not(scenario=='failure' and index==2)
        controller.end_task(ok)
        if scenario!='unknown':adapter.step_finished(index,ok)
        if not ok:
            adapter.plan_end(False);return
    if scenario=='unknown':adapter.single_end(True,True)
    else:adapter.plan_end(True)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='http://127.0.0.1:8789')
    parser.add_argument('--scenario',choices=('success','failure','pause','unknown','all'),default='success')
    parser.add_argument('--step-seconds',type=float,default=3)
    args=parser.parse_args(argv)
    if not HubClient(args.url).is_alive():
        print('请先运行 start_uah_hud.cmd，再启动演示。');return 2
    agent_id=f'uah-demo-{os.getpid()}'
    adapter=UhaNativeAdapter(HttpPublisher(args.url),agent_id=agent_id,agent_name='UAH 功能演示',heartbeat_s=1)
    adapter._explicit_progress=True
    adapter.runtime={'task_domain':'演示任务（不操作 UE）','dry_run':True,
                     'approval':'演示无需审批','soft_control':True,'hotkey_active':False}
    controller=SessionController()
    poller=ControlPoller(args.url,controller,agent_id=agent_id)
    adapter.start();adapter.attach_controller(controller);poller.start()
    print(f'演示 Agent：{agent_id}。若 HUD 显示其他任务，可从右键菜单选择。',flush=True)
    try:
        scenarios=('success','failure','unknown','pause') if args.scenario=='all' else (args.scenario,)
        for scenario in scenarios:
            print(f'场景：{scenario}',flush=True)
            run_demo(adapter,controller,scenario,max(.1,args.step_seconds))
            adapter.flush();time.sleep(2)
    except KeyboardInterrupt:
        controller.stop()
        adapter.plan_end(False)
    finally:
        poller.stop();adapter.stop()
    return 0


if __name__=='__main__':raise SystemExit(main())
