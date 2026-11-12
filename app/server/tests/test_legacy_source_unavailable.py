"""Synthetic frozen snapshots remain visible when source checks cannot run."""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from starlette.testclient import TestClient

from eval.lib.valuation_drafts import Draft, _json_bytes
from lynch_reader.archive import FrameworkArchive
from lynch_reader.auth.store import DeviceStore
from lynch_reader.catalog import catalog
from lynch_reader.legacy_batches import LocalBatchAuthorization
from lynch_reader.sandbox_valuation import SandboxProvider
from lynch_reader.valuation_reviews import Captured, ReviewService
from lynch_reader.web import create_app
from test_local_legacy_batch import Provider, Quotes


class LegacySourceUnavailable(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.devices = DeviceStore(self.root / 'devices.json')
        self.archive = FrameworkArchive(self.root / 'archive.json')
        self.provider = Provider()
        # Every fake snapshot below represents the fixed source revision v1.
        self.provider.frozen_generation = lambda ticker, draft: 'v1'
        self.service = ReviewService(self.root / 'reviews.sqlite', self.provider, self.devices)
        self.snapshots = {ticker: self.provider.snapshot(ticker) for ticker in self.provider.outputs}
        self.auth = LocalBatchAuthorization('fake-thread/turn-1', '同意迁移旧框架', 'fake/handoff.md',
                                            ('FAKE.US', 'NEXT.US'))
        self.docs = self.root / 'frameworks'
        self.docs.mkdir()
        for ticker in self.provider.outputs:
            (self.docs / (ticker + '.md')).write_text(
                f'# {ticker} — 虚构公司\n\n> 林奇：`stalwart` | 更新：2025-11-01\n', encoding='utf-8')

    def accept(self):
        return self.service.accept_legacy_batch(self.auth, archive=self.archive, snapshots=self.snapshots, evidence={})

    def rows(self):
        return {row['ticker']: row for row in catalog(self.docs, Quotes(), valuation_provider=self.service)['tickers']}

    def test_provider_current_error_before_accept_preserves_one_frozen_band_and_other_ticker(self):
        original = self.provider.current
        def fail_one(ticker, draft):
            if ticker == 'FAKE.US':
                raise OSError('synthetic source unavailable')
            return original(ticker, draft)
        self.provider.current = fail_one
        batch = self.accept()
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        self.assertEqual(batch['items']['NEXT.US'], 'accepted')
        row = self.rows()['FAKE.US']
        self.assertEqual(row['M'], 20)
        self.assertIn('valuation_source_unavailable', row['historical_evidence_flags'])
        self.assertEqual(self.service.current('FAKE.US')['status'], 'legacy_accepted')

    def test_provider_generation_or_currency_error_does_not_hide_valid_snapshot(self):
        for method in ('generation', 'currency'):
            with self.subTest(method=method):
                temp = TemporaryDirectory()
                self.addCleanup(temp.cleanup)
                self.service = ReviewService(Path(temp.name) / 'reviews.sqlite', self.provider, self.devices)
                original = getattr(self.provider, method)
                def fail_one(ticker, original=original):
                    if ticker == 'FAKE.US':
                        raise OSError('synthetic provider error')
                    return original(ticker)
                setattr(self.provider, method, fail_one)
                try:
                    batch = self.accept()
                    self.assertEqual(batch['items']['FAKE.US'], 'accepted')
                    self.assertEqual(batch['items']['NEXT.US'], 'accepted')
                    self.assertEqual(self.rows()['FAKE.US']['M'], 20)
                    self.assertIn('valuation_source_unavailable', self.rows()['FAKE.US']['historical_evidence_flags'])
                finally:
                    setattr(self.provider, method, original)

    def test_frozen_generation_failure_before_or_after_accept_keeps_snapshot(self):
        for before in (True, False):
            with self.subTest(before=before):
                temp = TemporaryDirectory()
                self.addCleanup(temp.cleanup)
                self.service = ReviewService(Path(temp.name) / 'reviews.sqlite', self.provider, self.devices)
                original = self.provider.frozen_generation
                def unavailable(*args):
                    raise OSError('synthetic frozen version unavailable')
                try:
                    if before:
                        self.provider.frozen_generation = unavailable
                    self.assertEqual(self.accept()['items']['FAKE.US'], 'accepted')
                    self.provider.frozen_generation = unavailable
                    result = self.service.current('FAKE.US')
                    self.assertEqual(result['status'], 'legacy_accepted')
                    self.assertEqual(result['output']['anchors']['M'], 20)
                    self.assertEqual(result['source_status'], 'unavailable')
                    self.assertFalse(result['source_changed_now'])
                    self.assertIn('valuation_source_unavailable', result['historical_evidence_flags'])
                finally:
                    self.provider.frozen_generation = original

    def test_old_snapshot_without_runtime_version_stays_visible_as_unverifiable(self):
        def missing_runtime_version(ticker, draft):
            return json.loads(draft._document)['runtime']['old_version']
        self.provider.frozen_generation = missing_runtime_version
        self.assertEqual(self.accept()['items']['FAKE.US'], 'accepted')
        result = self.service.current('FAKE.US')
        self.assertEqual(result['status'], 'legacy_accepted')
        self.assertEqual(result['output']['anchors']['M'], 20)
        self.assertEqual(result['source_status'], 'unavailable')
        self.assertFalse(result['source_changed_now'])

    def test_commit_recheck_error_is_recorded_even_if_provider_recovers(self):
        original = self.provider.current
        calls = 0
        def fail_second(ticker, draft):
            nonlocal calls
            if ticker == 'FAKE.US':
                calls += 1
                if calls == 2:
                    raise OSError('synthetic commit check error')
            return original(ticker, draft)
        self.provider.current = fail_second
        batch = self.accept()
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        row = self.rows()['FAKE.US']
        self.assertEqual(row['M'], 20)
        self.assertNotIn('valuation_source_unavailable', row['historical_evidence_flags'])
        self.assertIn('valuation_source_previously_unavailable', row['historical_evidence_flags'])

    def test_provider_error_after_accept_keeps_snapshot_in_real_home_route(self):
        self.assertEqual(self.accept()['items']['FAKE.US'], 'accepted')
        original = self.provider.current
        def fail_one(ticker, draft):
            if ticker == 'FAKE.US':
                raise RuntimeError('synthetic source unavailable')
            return original(ticker, draft)
        self.provider.current = fail_one
        app = create_app(self.docs, self.root / 'no-dist', Quotes(), self.devices, self.archive,
                         valuation_provider=self.service)
        with TestClient(app, base_url='http://testserver') as client:
            self.assertEqual(client.post('/api/pair', json={'code': self.devices.create_pairing('http')}).status_code, 204)
            rows = {row['ticker']: row for row in client.get('/api/tickers').json()['tickers']}
            self.assertEqual(rows['FAKE.US']['M'], 20)
            self.assertIn('valuation_source_unavailable', rows['FAKE.US']['historical_evidence_flags'])
            self.assertEqual(rows['NEXT.US']['M'], 20)

    def test_changed_source_before_or_after_accept_keeps_frozen_band_and_is_labeled(self):
        self.provider.current = lambda ticker, draft: ticker != 'FAKE.US'
        self.assertEqual(self.accept()['items']['FAKE.US'], 'accepted')
        self.assertEqual(self.rows()['FAKE.US']['M'], 20)
        self.assertIn('valuation_source_changed', self.rows()['FAKE.US']['historical_evidence_flags'])

    def test_invalid_snapshot_is_not_rescued_by_provider_failure(self):
        self.snapshots['FAKE.US'] = Captured(Draft(b'not-json'), None)
        self.provider.current = lambda *_: (_ for _ in ()).throw(OSError('synthetic failure'))
        batch = self.accept()
        self.assertEqual(batch['items']['FAKE.US'], 'invalid')
        self.assertIsNone(self.rows()['FAKE.US']['M'])

    def test_unavailable_source_can_recover_without_current_failure_warning(self):
        original = self.provider.current
        self.provider.current = lambda ticker, draft: (_ for _ in ()).throw(OSError()) if ticker == 'FAKE.US' else original(ticker, draft)
        self.assertEqual(self.accept()['items']['FAKE.US'], 'accepted')
        self.provider.current = original
        current = self.service.current('FAKE.US')
        self.assertEqual(current['source_status'], 'match')
        self.assertIn('valuation_source_previously_unavailable', current['historical_evidence_flags'])
        self.assertNotIn('valuation_source_unavailable', current['historical_evidence_flags'])
        self.assertEqual(self.rows()['FAKE.US']['M'], 20)

    def test_unavailable_check_prevents_replacement_even_if_generation_changed(self):
        self.assertEqual(self.accept()['items']['FAKE.US'], 'accepted')
        self.provider.revisions['FAKE.US'] = 'v2'
        original = self.provider.current
        self.provider.current = lambda ticker, draft: (_ for _ in ()).throw(OSError()) if ticker == 'FAKE.US' else original(ticker, draft)
        current = self.service.current('FAKE.US')
        self.assertEqual(current['source_status'], 'unavailable')
        self.assertFalse(current['source_changed_now'])
        self.assertNotIn('valuation_source_changed', current['historical_evidence_flags'])
        self.assertEqual(self.accept()['items']['FAKE.US'], 'existing')
        self.assertEqual(self.rows()['FAKE.US']['M'], 20)

    def test_bool_only_provider_is_unknown_and_cannot_trigger_replacement(self):
        self.assertEqual(self.accept()['items']['FAKE.US'], 'accepted')
        self.provider.source_status = None
        self.provider.current = lambda *_: False
        current = self.service.current('FAKE.US')
        self.assertEqual(current['source_status'], 'unavailable')
        self.assertFalse(current['source_changed_now'])
        self.assertEqual(self.accept()['items']['FAKE.US'], 'existing')

    def test_changed_source_then_recovery_distinguishes_historical_observation(self):
        original = self.provider.current
        self.provider.current = lambda ticker, draft: ticker != 'FAKE.US'
        self.assertEqual(self.accept()['items']['FAKE.US'], 'accepted')
        self.provider.current = original
        current = self.service.current('FAKE.US')
        self.assertEqual(current['source_status'], 'match')
        self.assertIn('valuation_source_previously_changed', current['historical_evidence_flags'])
        self.assertNotIn('valuation_source_changed', current['historical_evidence_flags'])

    def test_source_reverts_to_frozen_version_after_mismatching_acceptance(self):
        original = self.provider.source_status
        self.provider.revisions['FAKE.US'] = 'v2'
        self.provider.source_status = lambda ticker, draft: 'changed' if ticker == 'FAKE.US' else original(ticker, draft)
        self.assertEqual(self.accept()['items']['FAKE.US'], 'accepted')
        self.provider.revisions['FAKE.US'] = 'v1'
        self.provider.source_status = lambda ticker, draft: 'match' if ticker == 'FAKE.US' else original(ticker, draft)
        result = self.service.current('FAKE.US')
        self.assertEqual(result['source_status'], 'match')
        self.assertFalse(result['source_changed_now'])
        self.assertIn('valuation_source_previously_changed', result['historical_evidence_flags'])
        self.assertEqual(self.accept()['items']['FAKE.US'], 'existing')

    def test_unknown_frozen_generation_cannot_authorize_replacement(self):
        self.provider.frozen_generation = None
        self.provider.current = lambda ticker, draft: ticker != 'FAKE.US'
        self.assertEqual(self.accept()['items']['FAKE.US'], 'accepted')
        self.provider.current = lambda ticker, draft: True
        result = self.service.current('FAKE.US')
        self.assertEqual(result['source_status'], 'unavailable')
        self.assertFalse(result['source_changed_now'])
        self.assertEqual(self.accept()['items']['FAKE.US'], 'existing')

    def test_old_batch_without_observation_flags_remains_readable(self):
        batch = self.accept()
        with self.service._db() as con:
            encoded = con.execute('SELECT manifest FROM legacy_batches WHERE id=?', (batch['batch_id'],)).fetchone()[0]
            manifest = json.loads(encoded)
            manifest['items']['FAKE.US'].pop('source_observations')
            manifest['items']['FAKE.US']['source_observation_flags'] = ['valuation_source_changed']
            raw = json.dumps(manifest, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode()
            con.execute('DROP TRIGGER immutable_legacy_batch_update')
            con.execute('UPDATE legacy_batches SET manifest=?, digest=? WHERE id=?',
                        (raw, hashlib.sha256(raw).hexdigest(), batch['batch_id']))
        current = self.service.current('FAKE.US')
        self.assertEqual(current['source_status'], 'match')
        self.assertEqual(current['output']['anchors']['M'], 20)
        self.assertIn('valuation_source_previously_ambiguous', current['historical_evidence_flags'])
        self.assertNotIn('valuation_source_previously_changed', current['historical_evidence_flags'])

    def test_sandbox_source_read_error_is_unavailable_not_changed(self):
        provider = SandboxProvider.__new__(SandboxProvider)
        provider._snapshot = lambda *_: (_ for _ in ()).throw(OSError('synthetic read failure'))
        self.assertEqual(provider.source_status('FAKE.US', self.snapshots['FAKE.US'].draft), 'unavailable')
        self.assertFalse(provider.current('FAKE.US', self.snapshots['FAKE.US'].draft))

    def test_source_becoming_unavailable_during_replacement_keeps_original(self):
        self.assertEqual(self.accept()['items']['FAKE.US'], 'accepted')
        self.provider.revisions['FAKE.US'] = 'v2'
        old_version = self.service.current('FAKE.US')['batch_id']
        original = self.provider.source_status
        calls = 0
        def fail_on_commit(ticker, draft):
            nonlocal calls
            if ticker == 'FAKE.US':
                calls += 1
                if calls >= 3:
                    raise OSError('synthetic concurrent source failure')
            return original(ticker, draft)
        self.provider.source_status = fail_on_commit
        self.assertEqual(self.accept()['items']['FAKE.US'], 'existing')
        current = self.service.current('FAKE.US')
        self.assertEqual(current['batch_id'], old_version)
        self.assertEqual(current['source_status'], 'unavailable')
        self.assertEqual(self.rows()['FAKE.US']['M'], 20)

if __name__ == '__main__':
    unittest.main()
