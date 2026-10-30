"""Parser behaviour."""
import subprocess
import pytest
from test_lordd import ROOT, grok_home


@pytest.mark.parametrize('side', ['plugin'])
@pytest.mark.parametrize('shape', ['openers', 'fields'])
def test_r4_one_megabyte_of_notification_openers_parses_under_half_second(side, shape):
    text = '<task-notification>' * 60000 if shape == 'openers' else '<task-notification>' + '<result>' * 140000 + '</task-notification>'
    source = (ROOT / 'hooks/register.tsx').read_text()
    js = source[source.index('function task('):source.index('function testsError(')]
    for old, new in [('function task(text: string): Task', 'function task(text)'), ('const result: Task', 'const result'), ('new Set<string>()', 'new Set()'), ('(name: string)', '(name)')]:
        js = js.replace(old, new)
    command = ['/opt/homebrew/bin/node', '-e', js + '\nconst text=require("fs").readFileSync(0,"utf8"); const start=performance.now(); task(text); console.log((performance.now()-start)/1000)']
    try: result = subprocess.run(command, input=text, cwd=ROOT, capture_output=True, text=True, timeout=2)
    except subprocess.TimeoutExpired: pytest.fail(f'{side} parser exceeded two seconds on 1 MB of unmatched openers')
    assert result.returncode == 0, result.stderr
    elapsed = float(result.stdout)
    assert elapsed < .5, f'{side} parser took {elapsed:.3f} s'
