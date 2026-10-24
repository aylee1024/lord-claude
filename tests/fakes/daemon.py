"""Run protocol peers without applying a production sandbox to their audit files."""
import asyncio
import json
import os
from pathlib import Path
import runpy
import sys

real_start = asyncio.create_subprocess_exec


async def start(*argv, **kw):
    sandbox = None
    if '/usr/bin/sandbox-exec' in argv:
        at = argv.index('/usr/bin/sandbox-exec')
        end = at + 3
        while argv[end].startswith('-D'): end += 1
        assert argv[-4:] == ('off', 'agent', '--always-approve', 'stdio')
        assert argv[end] == sys.executable or Path(argv[end]).resolve() == Path(sys.executable).resolve()
        sandbox = {'profile': argv[at + 2], 'params': argv[at + 3:end], 'cwd': str(kw['cwd'])}
        argv = (*argv[:at], *argv[end:])
    process = await real_start(*argv, **kw)
    if sandbox:
        with (Path(os.environ['LORD_HOME']) / 'sandbox.jsonl').open('a') as file:
            file.write(json.dumps({'pid': process.pid, **sandbox}) + '\n')
    return process


asyncio.create_subprocess_exec = start
target = sys.argv.pop(1)
sys.argv[0] = target
runpy.run_path(target, run_name='__main__')
