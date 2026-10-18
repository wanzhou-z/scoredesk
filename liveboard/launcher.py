# -*- coding: utf-8 -*-
"""后台启动器：读取配置，日志、缓存和进程记录都放在本目录下。"""
import json
import os
import subprocess
import sys
from pathlib import Path
sys.dont_write_bytecode = True
BASE = Path(__file__).resolve().parent

def atomic_write_text(path, text):
    """临时文件 -> flush + fsync -> os.replace（进程记录也不许被蓝屏写坏）。"""
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('w', encoding='utf-8', newline='') as handle:
        handle.write(text); handle.flush(); os.fsync(handle.fileno())
    os.replace(tmp, path)

def main():
    cfg = json.loads((BASE / 'config.json').read_text(encoding='utf-8-sig'))
    env = os.environ.copy()
    env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1', TMP=str(BASE/'tmp'), TEMP=str(BASE/'tmp'), PIP_CACHE_DIR=str(BASE/'tmp'/'pip-cache'))
    dependencies = BASE / 'tmp' / 'pydeps'
    env['PYTHONPATH'] = str(dependencies) + os.pathsep + env.get('PYTHONPATH','')
    for folder in ('tmp', 'logs', 'snapshots'):
        (BASE/folder).mkdir(exist_ok=True)
    executable = Path(sys.executable)
    if executable.name.lower() == 'pythonw.exe':
        executable = executable.with_name('python.exe')
    processes = {}
    for name, arguments in [('server', []), ('poller', [])]:
        with (BASE/'logs'/(name+'.stdout.log')).open('ab') as stdout, (BASE/'logs'/(name+'.stderr.log')).open('ab') as stderr:
            process = subprocess.Popen([str(executable),'-B',str(BASE/(name+'.py')),*arguments],cwd=BASE,env=env,stdout=stdout,stderr=stderr,creationflags=subprocess.CREATE_NO_WINDOW)
            processes[name] = process.pid
    atomic_write_text(BASE/'tmp'/'started-processes.json', json.dumps({'port':cfg['port'],'processes':processes}))

if __name__ == '__main__':
    main()
