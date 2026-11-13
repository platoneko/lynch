"""Opt-in actual research-model reuse test; synthetic data and fake market CLI only."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import shutil
import sys
from types import SimpleNamespace

import pytest

from lynch_reader.chat.app_log import AppLog
from lynch_reader.chat.codex import CodexConfig, codex_env, command_allowed
from lynch_reader.chat.codex_link import CodexLink, discover_models
from tests.chat_helpers import LinkRecorder, conv

pytestmark = pytest.mark.skipif(os.environ.get('LYNCH_LIVE_RESEARCH') != '1', reason='real research model, opt-in')


def test_model_reuses_local_annual_cashflow_and_only_fetches_missing_quarter(tmp_path, monkeypatch):
    async def run():
        repo = Path(__file__).resolve().parents[3]
        legacy = tmp_path / 'existing/ALPHA.US/raw'
        legacy.mkdir(parents=True)
        annual = legacy / 'annual_cashflow.json'
        annual.write_text(json.dumps({'symbol': 'ALPHA.US', 'provider': 'synthetic', 'currency': 'USD',
                                      'source_ref': 'https://example.org/annual',
                                      'report_period': 'FY2025', 'cash_oper': '123.00', 'capex': '-45.00'}))
        (tmp_path / 'frameworks').mkdir()
        (tmp_path / 'frameworks/AGENTS.md').write_text('这是合成研究目录，只做资料复用测试，不写正式框架。')
        counts = tmp_path / 'queries.jsonl'
        fake = tmp_path / 'bin/longbridge'
        fake.parent.mkdir()
        fake.write_text(f'#!{sys.executable}\n' +
                        'import json,sys\nfrom pathlib import Path\n' +
                        f'p=Path({str(counts)!r})\n' +
                        'if "--help" in sys.argv or "help" in sys.argv:\n print("financial-statement SYMBOL --kind CF --report af|qf --format json"); sys.exit(0)\n' +
                        'period=sys.argv[sys.argv.index("--report")+1] if "--report" in sys.argv else "unknown"\n' +
                        'with p.open("a") as f: f.write(json.dumps({"period":period,"args":sys.argv[1:]})+"\\n")\n' +
                        'print(json.dumps({"symbol":"ALPHA.US","provider":"synthetic","currency":"USD",'
                        '"source_ref":"https://example.org/quarter","report_period":"Q32026","cash_oper":"40.00","capex":"-15.00"}))\n')
        fake.chmod(0o755)
        store = tmp_path / 'archive'
        env = codex_env(os.environ)
        env.update(LYNCH_RESEARCH_DIR=str(store), PYTHONPATH=str(repo),
                   PATH=str(fake.parent) + os.pathsep + env.get('PATH', ''))
        cfg = CodexConfig((shutil.which('codex') or 'codex',), tmp_path, tmp_path / 'app-logs')
        models = await discover_models(cfg, env)
        chosen = next((m for m in models if m.id == 'gpt-6.1-terra'), next((m for m in models if m.is_default), models[0]))
        c = SimpleNamespace(**{**asdict(conv(agent='codex', kind='research', ticker=None,
                                             model=chosen.id, effort=chosen.default_effort)), 'codex_thread': None})
        rec = LinkRecorder()
        log = AppLog(cfg.logs / 'research.jsonl')
        link = CodexLink(cfg, c, env, log, rec.events())
        original_call = link._rpc.call

        async def configured_call(method, params, **kwargs):
            if method == 'thread/start':
                params = dict(params)
                params['developerInstructions'] += (
                    f'\n测试环境说明：已有研究目录为 {tmp_path / "existing"}；这是数据位置，不保证覆盖所问时期。'
                    f'本测试的 Longbridge 可执行文件是 {fake}，仅使用它查询合成资料；不连接任何真实行情、网页或 MCP。'
                    f'被测模块可通过环境 PYTHONPATH={repo} 访问。描述JSON写在当前测试目录内。')
                params['config'] = {**params.get('config', {}), 'web_search': 'disabled'}
            return await original_call(method, params, **kwargs)

        monkeypatch.setattr(link._rpc, 'call', configured_call)
        original_request = link._on_request

        def restricted_request(rpc_id, method, params):
            if method == 'item/commandExecution/requestApproval':
                command = params.get('command', '')
                text = ' '.join(command) if isinstance(command, list) else command
                denied = any(word in text for word in ('curl ', 'wget ', 'httpx', 'requests.', 'urllib.', 'printenv', 'env |'))
                # Absolute fake path is required; never accept a request to the real CLI.
                invoking_longbridge = re.search(r'''(?:^|[\s'"/])longbridge(?:[\s'"]|$)''', text)
                if (invoking_longbridge and str(fake) not in text) or denied or not command_allowed(command):
                    link._rpc.reply(rpc_id, {'decision': 'decline'})
                    return
            return original_request(rpc_id, method, params)

        monkeypatch.setattr(link, '_on_request', restricted_request)

        async def turn(text):
            before = len(rec.turn_ends)
            await link.prompt(text)
            async with asyncio.timeout(240):
                while len(rec.turn_ends) == before:
                    for permission in rec.permissions:
                        await link.answer(permission[0], False)
                    await asyncio.sleep(0.1)
            assert rec.turn_ends[-1][1] is None, rec.turn_ends[-1]

        try:
            c.codex_thread = await link.start()
            await turn('请比较合成公司 ALPHA.US 的 FY2025 和 Q32026 经营现金流、资本开支、自由现金流。'
                       '请给出数据依据；研究资料值得保存，但不要写正式估值框架。')
            queries = [json.loads(line) for line in counts.read_text().splitlines()] if counts.exists() else []
            assert [q['period'] for q in queries] == ['qf'], queries
            from eval.lib.research_store import find_materials
            saved = find_materials(store, {'symbol': ['ALPHA.US']})
            assert any('FY2025' in m.get('report_periods', []) for m in saved), saved
            assert any(m['material_id'] in ''.join(rec.deltas) for m in saved), ''.join(rec.deltas)
            tool_commands = [b['input'].get('command', '') for e in rec.entries for b in e['message']['content']
                             if isinstance(b, dict) and b.get('type') == 'tool_use' and b.get('name') == 'Bash']
            assert any('discover' in str(command) for command in tool_commands), tool_commands
            before = len(queries)
            await turn('把刚才的现金流比较改成一张表，注明所用的资料编号。')
            queries = [json.loads(line) for line in counts.read_text().splitlines()]
            assert len(queries) == before, queries
            assert annual.exists() and json.loads(annual.read_text())['cash_oper'] == '123.00'
            print(json.dumps({'model': chosen.id, 'annual_requests': 0, 'quarter_requests': 1,
                              'materials': [m['material_id'] for m in saved], 'reformat_requests': len(queries)-before}))
        finally:
            await link.close()

    asyncio.run(run())
