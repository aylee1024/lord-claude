"""Redaction behaviour."""
import asyncio
import random
import string
import subprocess
import pytest
from test_lordd import PYTHON, ROOT, grok_home, load
from delegation_helpers import scope


PREFIXES = ['Bearer ', 'Authorization: Bearer ', 'api_token: ', '"access_token": "',
            'client_secret=', 'sk-', 'eyJ', *['gh' + c + '_' for c in 'pousr'], 'github_pat_', 'AKIA', 'AIza', 'xoxb-', 'xai-']

@pytest.mark.parametrize('shape', ['eyJ-', '-', 'token-'])
def test_r2_redaction_128kb_finishes_under_half_second(shape):
    code = ('import importlib.util,time; '
            's=importlib.util.spec_from_file_location("probe","lordd.py"); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); '
            f'text=({shape!r}*131072)[:131072]; start=time.perf_counter(); m.Redactor()(text); print(time.perf_counter()-start)')
    try: result = subprocess.run([PYTHON, '-c', code], cwd=ROOT, capture_output=True, text=True, timeout=2)
    except subprocess.TimeoutExpired: pytest.fail('128 KB redaction exceeded two seconds')
    assert result.returncode == 0, result.stderr
    assert float(result.stdout) < .5


def test_r2_redact_then_trim_sites_bound_input(tmp_path, monkeypatch):
    m = load('r2_trim_margin'); m.HOME = tmp_path
    sizes = []
    original = m.REDACT
    def redact(text, **kwargs): sizes.append(len(str(text))); return original(text, **kwargs)
    monkeypatch.setattr(m, 'REDACT', redact)
    async def scenario():
        a = m.Agent({'agentId': 'a', 'family': 'gpt', 'cwd': '/tmp/x', 'model': 'fake'})
        a.state = 'running'; a.note('x' * 131072)
        a.read_paths([('read', '/' + 'x' * 131072)])
        a.row('r', 'command', 'x' * 131072)
        a.row_done('r', 'x' * 131072 + '\nTAIL')
        assert (await a.rows['r'])['output'].endswith('TAIL')
        d = object.__new__(m.Daemon); d.redact = redact; d.log('x' * 131072)
    asyncio.run(scenario())
    assert max(sizes) <= 16384, sizes


@pytest.mark.parametrize('site', ['row', 'stderr'])
def test_r4_cut_drops_long_secret_word_before_redacting(scope, site):
    d, a, m, *_ = scope
    text = 'Bearer ' + 'Q' * 14000 + '\nvisible result'
    async def scenario():
        if site == 'row':
            a.rows['row'] = asyncio.get_running_loop().create_future()
            a.row_done('row', text)
            return (await a.rows['row'])['output']
        vendor = m.Vendor(d, 'gpt', a)
        reader = asyncio.StreamReader(); reader.feed_data((text + '\n').encode()); reader.feed_eof()
        process = type('Process', (), {'stderr': reader})()
        vendor.exec_process = process
        await vendor.stderr(process)
        return vendor.exec_error
    shown = asyncio.run(scenario())
    assert 'Q' * 20 not in shown and 'visible result' in shown


@pytest.mark.parametrize('site', ['row', 'stderr'])
@pytest.mark.parametrize('prefix', PREFIXES)
def test_r5_every_credential_prefix_cut_is_redacted(scope, monkeypatch, site, prefix):
    d, a, m, *_ = scope
    limit = m.ROW_OUTPUT
    secret = 'Q' * (limit - 1200)
    if prefix == 'AKIA': secret = 'Q' * 16
    if prefix == 'eyJ':
        payload = min(8192, limit - 1300)
        secret = 'Q' * 30 + '.' + 'Q' * payload + '.' + 'Q' * (limit - 1232 - payload)
    line = prefix + secret
    original, redacted = m.REDACT, []
    assert 'Q' * 16 not in original(line)
    def redact(value, **kwargs):
        result = original(value, **kwargs); redacted.append(result[0] if isinstance(result, tuple) else result); return result
    monkeypatch.setattr(m, 'REDACT', redact); d.redact = redact; d.log = lambda text: None
    async def run():
        for case in range(2 * len(prefix)):
            nearby, offset = divmod(case, len(prefix))
            redacted.clear()
            trailer = ' ' * (limit - (len(line) - offset))
            full = ('earlier output\n' if nearby else 'earlier output ') * 30 + line + trailer
            assert full[-limit:].startswith(line[offset:])
            if site == 'row':
                a.rows['row'] = asyncio.get_running_loop().create_future(); a.row_done('row', full)
                shown = (await a.rows['row'])['output']
            else:
                vendor = m.Vendor(d, 'gpt', a)
                reader = asyncio.StreamReader(); reader.feed_data(full.encode()); reader.feed_eof()
                process = type('Process', (), {'stderr': reader})(); vendor.exec_process = process
                await vendor.stderr(process); shown = vendor.exec_error
            assert all('Q' * 16 not in value for value in [shown, *redacted]), f'{site}: prefix {prefix!r}, offset {offset}'
    asyncio.run(run())


def test_r5_cut_keeps_redaction_and_discards_only_the_partial_run(scope):
    _, _, m, *_ = scope
    assert m.redact_tail('old\nBearer decoy\nend', 18, m.REDACT).endswith('[REDACTED]\nend')
    assert m.redact_tail('old\n' + 'x' * 300 + ' Bearer decoy\nend', 18, m.REDACT).endswith('[REDACTED]\nend')


@pytest.mark.parametrize('shape', ['json', 'progress'])
@pytest.mark.parametrize('site', ['row', 'stderr'])
def test_r6_long_line_keeps_its_tail_and_counts_the_omission(scope, shape, site):
    d, a, m, *_ = scope
    text = '{"result":"' + 'v' * 30000 + '","status":"FAILED: disk full"}' if shape == 'json' else '#' * 30000 + '\r99% still running'
    async def run():
        if site == 'row':
            a.rows['row'] = asyncio.get_running_loop().create_future()
            a.row_done('row', text)
            shown = (await a.rows['row'])['output']
            assert ('FAILED: disk full' if shape == 'json' else '99% still running') in shown
            note, tail = shown.split('\n', 1)
            omitted = int(note.split()[0][1:])
            assert omitted + len(tail) == len(text)
        else:
            vendor = m.Vendor(d, 'gpt', a); d.log = lambda text: None
            reader = asyncio.StreamReader(); reader.feed_data(text.encode()); reader.feed_eof()
            process = type('Process', (), {'stderr': reader})(); vendor.exec_process = process
            await vendor.stderr(process)
            assert ('FAILED: disk full' if shape == 'json' else '99% still running') in vendor.exec_error
    asyncio.run(run())


@pytest.mark.parametrize('site', ['row', 'stderr'])
@pytest.mark.parametrize('prefix', PREFIXES)
def test_r6_prefix_and_value_cuts_keep_a_safe_tail(scope, site, prefix):
    d, a, m, *_ = scope
    secret = 'Q' * 800
    if prefix == 'AKIA': secret = 'Q' * 16
    if prefix == 'eyJ': secret = 'Q' * 30 + '.' + 'Q' * 600 + '.' + 'Q' * 100
    line = prefix + secret
    assert 'Q' * 16 not in m.REDACT(line)
    async def run():
        for offset in [*range(len(prefix)), len(prefix) + 1, len(prefix) + 63, len(prefix) + 128]:
            if offset >= len(line): continue
            trailer = ' ' * (2000 - (len(line) - offset) - 10) + ' tail kept'
            text = 'earlier ' * 1000 + line + trailer
            if site == 'row':
                a.rows['r'] = asyncio.get_running_loop().create_future(); a.row_done('r', text)
                shown = (await a.rows['r'])['output']
            else:
                vendor = m.Vendor(d, 'gpt', a); d.log = lambda text: None
                reader = asyncio.StreamReader(); reader.feed_data(text.encode()); reader.feed_eof()
                process = type('Process', (), {'stderr': reader})(); vendor.exec_process = process
                await vendor.stderr(process); shown = vendor.exec_error
            assert 'Q' * 16 not in shown and shown.endswith('tail kept'), (prefix, offset)
    asyncio.run(run())


def test_r7_multiline_environment_secret_stays_out_of_rows(scope, monkeypatch):
    _, a, m, *_ = scope
    rng = random.Random(7)
    body = '\n'.join(''.join(rng.choice(string.ascii_letters + string.digits + '+/') for _ in range(64)) for _ in range(26))
    secret = '-----BEGIN PRIVATE KEY-----\n' + body + '\n-----END PRIVATE KEY-----'
    monkeypatch.setenv('DEPLOY_PRIVATE_KEY', secret)
    monkeypatch.setattr(m, 'REDACT', m.Redactor())
    output = 'build log\n' * 50 + 'DEPLOY_PRIVATE_KEY=' + secret + '\n' + 'done: 42 files\n' * 40
    async def run():
        a.rows['r'] = asyncio.get_running_loop().create_future()
        a.row_done('r', output)
        shown = (await a.rows['r'])['output']
        assert not set(body.splitlines()).intersection(shown.splitlines())
        assert shown.endswith('done: 42 files\n')
    asyncio.run(run())


@pytest.mark.parametrize('lines', [160, 900, 1100])
def test_r8_long_exact_secret_uses_bounded_lookback(scope, monkeypatch, lines):
    _, a, m, *_ = scope; rng = random.Random(11)
    body = '\n'.join(''.join(rng.choice(string.ascii_letters + string.digits + '+/') for _ in range(64)) for _ in range(lines))
    secret = '-----BEGIN CERTIFICATE CHAIN KEY-----\n' + body + '\n-----END CERTIFICATE CHAIN KEY-----'
    original = m.Redactor(); original.secrets.add(secret); sizes = []
    def redact(value, **kwargs): sizes.append(len(value)); return original(value, **kwargs)
    redact.secrets = original.secrets; monkeypatch.setattr(m, 'REDACT', redact)
    output = 'build log\n' * 1000 + 'BUNDLE_PRIVATE_KEY=' + secret + '\n' + 'done: 42 files\n' * 40
    async def run():
        a.rows['r'] = asyncio.get_running_loop().create_future(); a.row_done('r', output)
        shown = (await a.rows['r'])['output']
        if len(secret) <= 65536: assert not set(body.splitlines()).intersection(shown.splitlines())
        assert shown.endswith('done: 42 files\n')
        assert m.ROW_OUTPUT + 8192 < sizes[-1] <= m.ROW_OUTPUT + 65536
    asyncio.run(run())
