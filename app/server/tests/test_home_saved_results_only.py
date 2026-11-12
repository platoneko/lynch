"""The home page consumes saved prices without executing valuation models."""
import base64
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

from eval.lib import valuation_runner
from eval.lib.price import Quote
from eval.lib.result_service import open_result_reader
from eval.lib.valuation_drafts import Draft, _json_bytes
from lynch_reader.archive import FrameworkArchive
from lynch_reader.catalog import QuoteBatch, QuoteSnapshot
from lynch_reader.legacy_batches import LocalBatchAuthorization
from lynch_reader.sandbox_valuation import SandboxConfig, SandboxProvider
from lynch_reader.valuation_reviews import Captured, ReviewService
from lynch_reader.web import create_app


def test_home_load_and_refresh_read_saved_bands_without_running_models(tmp_path):
    docs = tmp_path / 'docs'
    code = tmp_path / 'code'
    content = tmp_path / 'content'
    docs.mkdir()
    code.mkdir()
    content.mkdir()
    (docs / 'FAKE.US.md').write_text(
        '# FAKE.US — Example\n\n> 林奇：`stalwart` | 更新：2026-01-01\n', encoding='utf-8')
    provider = SandboxProvider(SandboxConfig(code, content, None, tmp_path, tmp_path / 'registry',
                                              {'FAKE.US': 'USD'}), read_only=True)
    database = tmp_path / 'results.sqlite'
    service = ReviewService(database, provider, None)
    archive = FrameworkArchive(tmp_path / 'archive.json')
    output = {'ticker': 'FAKE.US', 'currency': 'USD', 'valuation_as_of': '2026-01-01',
              'framework_as_of': None, 'units': {'price': 'USD_PER_SHARE'},
              'anchors': {'L': 10, 'M': 20, 'H': 30}, 'candidate': None, 'option': None}
    raw = _json_bytes(output)
    entry = 'historical/FAKE.US.json'
    document = {'format': 1, 'entry': entry, 'inputs': {entry: base64.b64encode(raw).decode()},
                'runtime': {'source': 'historical_saved_output', 'execution_verified': False,
                            'script_missing': True, 'source_status': None, 'script_status': 'missing',
                            'script_sha256': None, 'history_notes': []}, 'output': output}
    captured = Captured(Draft(_json_bytes(document)), None)
    authorization = LocalBatchAuthorization('test', 'migrate', 'test', ('FAKE.US',))
    assert service.accept_legacy_batch(authorization, archive=archive,
                                       snapshots={'FAKE.US': captured}, evidence={})['items'] == {
                                           'FAKE.US': 'accepted'}

    class Quotes:
        def read(self, symbols):
            quote = Quote(symbol='FAKE.US', last=20, prev_close=19, status='regular')
            return QuoteSnapshot(QuoteBatch(tuple(symbols), {'FAKE.US': quote}, 1700000000), None)

    reader = open_result_reader(database=database)
    app = create_app(docs, tmp_path / 'no-web', Quotes(), None, archive, valuation_provider=reader)
    handler = next(route.endpoint for route in app.routes if getattr(route, 'path', None) == '/api/tickers')
    with patch.dict(os.environ, {'LYNCH_VALUATION_SOURCES': ''}), \
         patch.object(valuation_runner, 'run', side_effect=AssertionError('model execution forbidden')), \
         patch.object(SandboxProvider, 'calculate', side_effect=AssertionError('calculation forbidden')), \
         patch('subprocess.run', side_effect=AssertionError('subprocess forbidden')):
        first = handler(SimpleNamespace(headers={}))
        row = json.loads(first.body)['tickers'][0]
        assert (row['L'], row['M'], row['H']) == (10, 20, 30)
        assert row['band']['zone'] == 'neutral'
        assert handler(SimpleNamespace(headers={'if-none-match': first.headers['etag']})).status_code == 304
