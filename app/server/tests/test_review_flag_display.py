"""Valuation doubts belong in the document, not in homepage display gates."""
import time
from datetime import datetime, timezone

import pytest

from eval.lib.result_view import map_result
from eval.lib.zones import zone_of
from lynch_reader.catalog import Quote, QuoteBatch, _price_row


@pytest.mark.parametrize('flags', [
    ['historical_double_counting'], ['historical_reference_only'], [],
])
def test_review_flags_do_not_hide_saved_valuation_zones(flags):
    record = {'status': 'legacy_accepted', 'origin': 'local_operator_attestation',
        'batch_id': 'fictional',
        'historical_comparison': {'candidate': {'state': 'present'}, 'option': {'state': 'present'},
                                  'review_flags': {'state': 'known', 'values': []}},
        'historical_evidence_source': {'reference': 'fictional.json', 'rationale': 'test'},
        'output': {
        'ticker': '1810.HK', 'currency': 'HKD',
        'anchors': {'L': 22.24, 'M': 28.26, 'H': 38.74},
        'candidate': {'label': 'test', 'anchors': {'L': 24, 'M': 30, 'H': 40}},
        'option': {'anchors': {'L': 25, 'M': 32, 'H': 42}},
        'review_flags': flags,
    }}
    _, result = map_result('1810.HK', record)
    assert result is not None
    row = {}
    quote = Quote(symbol='1810.HK', last=30, prev_close=29, status='regular')
    _price_row(row, '1810.HK', result,
               QuoteBatch(('1810.HK',), {'1810.HK': quote}, time.time()),
               datetime.now(timezone.utc))
    assert row['band']['zone'] == zone_of(30, 22.24, 28.26, 38.74)
    assert row['band']['candidate_zone'] == zone_of(30, 24, 30, 40)
    assert row['band']['option_zone'] == zone_of(30, 25, 32, 42)
    assert list(result.review_flags) == flags
