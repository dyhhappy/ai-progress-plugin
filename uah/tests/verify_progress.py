"""One-command local verification; no Unreal, input injection or desktop capture."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-widgets',action='store_true',help='Skip hidden Tk tests on headless installations')
    args=parser.parse_args(argv)
    root=Path(__file__).resolve().parents[2]
    output=root/'artifacts'/'uah-progress'/time.strftime('%Y%m%d-%H%M%S')
    output.mkdir(parents=True,exist_ok=True)
    suites=[
        ('syntax',['-m','compileall','-q','uah','src','uha.py']),
        ('hub-selftest',['-m','uah.tools.uah','selftest']),
        ('progress',['-m','uah.tests.test_progress_completion']),
        ('legacy-hud',['-m','uah.tests.test_uah_phase1','1','2','3','5','6','7','8','9','11','12']),
        ('uha-offline',['tests/test_offline.py']),
        ('uha-router',['tests/test_router.py']),
        ('uha-controls',['tests/test_phase3.py']),
    ]
    if not args.no_widgets:
        suites.append(('hidden-widgets',['-m','uah.tests.test_hud_widgets']))
    env={**os.environ,'PYTHONUTF8':'1'}
    results=[]
    for name,command in suites:
        started=time.monotonic()
        print(f'RUN {name}',flush=True)
        try:
            proc=subprocess.run([sys.executable,*command],cwd=root,env=env,
                capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=120,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            code=proc.returncode;log=proc.stdout+proc.stderr
        except subprocess.TimeoutExpired:
            code=124;log='Test timed out after 120 seconds.\n'
        (output/f'{name}.log').write_text(log,encoding='utf-8')
        results.append({'suite':name,'exit_code':code,'seconds':round(time.monotonic()-started,2)})
        print(f'{"PASS" if code==0 else "FAIL"} {name}',flush=True)
        if code:print(log[-5000:],flush=True)
    passed=all(r['exit_code']==0 for r in results)
    summary={'passed':passed,'python':sys.executable,'python_version':sys.version,
             'suites':results,'widgets_skipped':args.no_widgets,
             'not_tested':['physical multi-monitor drag/hotplug','physical emergency hotkey/input release',
                           'real Unreal scene execution','historical src.safety/ApprovalGate integration (source not supplied)']}
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'\n{ "PASS" if passed else "FAIL"}: {sum(r["exit_code"]==0 for r in results)}/{len(results)} suites')
    print(f'Logs: {output}')
    return 0 if passed else 1


if __name__=='__main__':raise SystemExit(main())
