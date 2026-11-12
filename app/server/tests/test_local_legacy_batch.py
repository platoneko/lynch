"""Synthetic local migration batches; no HTTP grant and no real stock models."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import multiprocessing
from pathlib import Path
import sqlite3
import threading
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient
from eval.lib.valuation_drafts import Draft, _json_bytes
from lynch_reader.auth.store import DeviceStore
from lynch_reader.archive import FrameworkArchive
from lynch_reader.catalog import QuoteSnapshot, catalog
from lynch_reader.valuation_reviews import Captured, Problem, ReviewService
from lynch_reader.legacy_batches import HistoricalEvidence, LocalBatchAuthorization
from lynch_reader.web import create_app


def result(ticker):
    return {'ticker': ticker, 'currency': 'USD', 'valuation_as_of': None, 'framework_as_of': None,
            'historical_valuation_as_of': '2025-12-31', 'historical_research_as_of': '2026-01-01',
            'units': {'price': 'USD_PER_SHARE'}, 'anchors': {'L': 10, 'M': 20, 'H': 30},
            'candidate': None, 'option': None, 'legacy_concerns': ['在正文披露']}


class Provider:
    def __init__(self):
        self.outputs = {'FAKE.US': result('FAKE.US'), 'NEXT.US': result('NEXT.US')}
        self.revisions = {ticker: 'v1' for ticker in self.outputs}
        self.calculations = []

    def generation(self, ticker):
        return self.revisions[ticker]

    def currency(self, ticker):
        return 'USD'

    def calculate(self, ticker):
        self.calculations.append(ticker)
        return self.snapshot(ticker)

    def snapshot(self, ticker):
        raw = self.outputs[ticker]
        snapshot = {'format': 1, 'entry': ticker + '.py',
                    'inputs': {ticker + '.py': base64.b64encode(b'synthetic-source').decode()},
                    'runtime': {}, 'output': raw}
        return Captured(Draft(_json_bytes(snapshot)), None)

    def current(self, ticker, draft):
        return self.outputs.get(ticker) == draft.output

    def source_status(self, ticker, draft):
        return 'match' if self.current(ticker, draft) else 'changed'


class Quotes:
    def read(self, symbols):
        return QuoteSnapshot(None, None)


def archive_from_process(path, started, finished):
    archive = FrameworkArchive(Path(path))
    started.set()
    archive.archive('FAKE.US')
    finished.set()


class LocalBatch(unittest.TestCase):
    def test_axis_supplement_attaches_only_to_historical_result(self):
        from eval.lib.axis_history import accept_history
        from eval.lib.axis_projection import project_axes
        from eval.lib.test_axis_history_synthetic import payload
        self.service.accept_legacy_batch(self.auth, archive=self.archive, snapshots=self.snapshots, evidence=self.evidence)
        axes = payload()
        axes['axis_metadata']['example']['priced_mounts'] = ['FAKE.US']
        with self.service._db() as con:
            version = self.service._version(con, 'FAKE.US')
            accept_history(con, 'FAKE.US', version, axes)
        saved = self.service.current('FAKE.US')
        self.assertNotIn('axis_metadata', saved['output'])
        projected = project_axes('FAKE.US', saved)
        self.assertEqual(projected['status'], 'known')
        self.assertEqual(projected['axes'][0]['name'], 'example')

    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.devices = DeviceStore(self.root / 'devices.json')
        self.provider = Provider()
        self.snapshots = {ticker: self.provider.snapshot(ticker) for ticker in self.provider.outputs}
        self.evidence = {}
        for ticker in self.provider.outputs:
            self.write_evidence(ticker)
        self.service = ReviewService(self.root / 'reviews.sqlite', self.provider, self.devices)
        self.archive = FrameworkArchive(self.root / 'archive.json')
        self.auth = LocalBatchAuthorization(
            session_reference='synthetic-thread/turn-7',
            user_authorization='同意按原有数值迁移未归档标的，疑点写入文档',
            workline_reference='synthetic-workline/handoff.md',
            scope=('FAKE.US', 'NEXT.US', 'MISSING.US'),
        )
        self.docs = self.root / 'frameworks'
        self.docs.mkdir()
        (self.docs / 'FAKE.US.md').write_text(
            '# FAKE.US — 假公司\n\n> 林奇：`stalwart` | 更新：2025-11-01\n', encoding='utf-8')

    def write_evidence(self, ticker):
        historical = self.root / (ticker + '-old-result.json')
        script = self.root / (ticker + '.py')
        historical.write_bytes(_json_bytes(self.provider.outputs[ticker]))
        script.write_text('# historical source for ' + ticker, encoding='utf-8')
        self.evidence[ticker] = HistoricalEvidence(historical, script, '对照历史记录与迁移快照')

    def write_manifest(self, ticker, entries):
        old = self.evidence[ticker]
        path = self.root / (ticker + '-manifest.json')
        body = {'ticker': ticker, 'record_sha256': hashlib.sha256(old.result_path.read_bytes()).hexdigest(), **entries}
        path.write_bytes(_json_bytes(body))
        self.evidence[ticker] = HistoricalEvidence(old.result_path, old.script_path, old.rationale, path)

    def row(self):
        return catalog(self.docs, Quotes(), valuation_provider=self.service)['tickers'][0]

    def assert_visible_difference(self, batch, field):
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        row = self.row()
        self.assertIn('historical_values_differ', row['historical_evidence_flags'])
        matches = [item for item in row['historical_evidence_differences'] if item['field'] == field]
        self.assertTrue(matches, (field, row['historical_evidence_differences']))
        return matches[0]

    def test_local_batch_preserves_history_and_does_not_invent_device_events(self):
        before = self.row()
        self.assertEqual(before['valuation_status'], 'missing')
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive, snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        self.assertEqual(batch['items']['NEXT.US'], 'accepted')
        self.assertEqual(batch['items']['MISSING.US'], 'missing')
        current = self.service.current('FAKE.US')
        self.assertEqual(current['status'], 'legacy_accepted')
        self.assertEqual(current['origin'], 'local_operator_attestation')
        self.assertEqual(current['verification'], 'historical_unverified')
        self.assertIn('historical_material_unreviewed', current['historical_evidence_flags'])
        self.assertNotIn('confirmed_at', current)
        self.assertIsNone(current['output']['framework_as_of'])
        row = self.row()
        self.assertEqual(row['valuation_status'], 'legacy_accepted')
        self.assertEqual(row['M'], 20)
        self.assertEqual(row['framework_updated'], '2025-11-01')
        with self.service._db() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM events').fetchone()[0], 0)
            stored = con.execute('SELECT manifest FROM legacy_batches WHERE id=?', (batch['batch_id'],)).fetchone()[0]
        self.assertIn(b'synthetic-thread/turn-7', stored)
        self.assertIn(hashlib.sha256(b'synthetic-source').hexdigest().encode(), stored)
        decoded = json.loads(stored)
        self.assertEqual(decoded['user_authorization'], self.auth.user_authorization)
        self.assertEqual(decoded['origin'], 'local_operator_attestation')

    def test_later_available_ticker_can_be_added_without_replacing_prior_results(self):
        first = self.service.accept_legacy_batch(self.auth, archive=self.archive, snapshots=self.snapshots, evidence=self.evidence)
        self.provider.outputs['MISSING.US'] = result('MISSING.US')
        self.provider.revisions['MISSING.US'] = 'v1'
        self.snapshots['MISSING.US'] = self.provider.snapshot('MISSING.US')
        self.write_evidence('MISSING.US')
        self.provider.calculations.clear()
        second = self.service.accept_legacy_batch(self.auth, archive=self.archive, snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(second['items']['FAKE.US'], 'existing')
        self.assertEqual(second['items']['MISSING.US'], 'accepted')
        self.assertNotIn('FAKE.US', self.provider.calculations)
        self.assertEqual(self.service.current('FAKE.US')['batch_id'], first['batch_id'])
        self.assertEqual(self.service.current('MISSING.US')['batch_id'], second['batch_id'])

    def test_batch_records_and_snapshots_cannot_be_updated_or_deleted(self):
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive, snapshots=self.snapshots, evidence=self.evidence)
        with self.service._db() as con:
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute('UPDATE legacy_batches SET manifest=? WHERE id=?', (b'{}', batch['batch_id']))
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute('DELETE FROM legacy_items WHERE batch_id=?', (batch['batch_id'],))

    def test_source_changes_keep_the_frozen_result_with_a_warning(self):
        self.service.accept_legacy_batch(self.auth, archive=self.archive, snapshots=self.snapshots, evidence=self.evidence)
        self.provider.revisions['FAKE.US'] = 'v2'
        self.assertEqual(self.service.current('FAKE.US')['status'], 'legacy_accepted')
        self.assertEqual(self.row()['M'], 20)
        self.assertIn('valuation_source_changed', self.row()['historical_evidence_flags'])

    def test_source_change_during_preparation_publishes_labeled_snapshot(self):
        original = self.provider.current
        def change_after_capture(ticker, draft):
            valid = original(ticker, draft)
            if ticker == 'FAKE.US':
                self.provider.revisions[ticker] = 'v2'
            return valid
        self.provider.current = change_after_capture
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive, snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        self.assertEqual(self.row()['M'], 20)
        self.assertIn('valuation_source_changed', self.row()['historical_evidence_flags'])

    def test_concurrent_batches_publish_at_most_one_snapshot_per_ticker(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            batches = list(executor.map(lambda _: self.service.accept_legacy_batch(
                self.auth, archive=self.archive, snapshots=self.snapshots, evidence=self.evidence), range(2)))
        self.assertEqual(sorted(batch['items']['FAKE.US'] for batch in batches), ['accepted', 'existing'])
        with self.service._db() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM legacy_items WHERE ticker='FAKE.US'").fetchone()[0], 1)

    def test_plain_untrusted_string_and_absent_scope_cannot_authorize_batch(self):
        with self.assertRaises((Problem, TypeError, ValueError)):
            self.service.accept_legacy_batch('I say yes', archive=self.archive, snapshots=self.snapshots, evidence=self.evidence)
        with self.assertRaises((Problem, TypeError, ValueError)):
            self.service.accept_legacy_batch(LocalBatchAuthorization('', '', '', ()), archive=self.archive, snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(self.service.current('FAKE.US')['status'], 'missing')

    def test_no_network_write_route_is_added_for_batch_authorization(self):
        from lynch_reader.valuation_reviews import review_routes
        paths = {route.path for route in review_routes(self.service, self.devices)}
        self.assertFalse(any('batch' in path for path in paths))

    def test_batch_does_not_replace_existing_formally_confirmed_research(self):
        token = self.devices.redeem(self.devices.create_pairing('synthetic'))
        actor = hashlib.sha256(token.encode()).hexdigest()
        self.service._sandbox_verifier = type('Verifier', (), {'require_current': lambda *_: None})()
        rid = self.service.create('FAKE.US', actor)['review_id']
        view = self.service.view(rid, actor)
        self.assertEqual(self.service.confirm(rid, token, view['csrf'])[0], 201)
        self.provider.calculations.clear()
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive, snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'existing')
        self.assertNotIn('FAKE.US', self.provider.calculations)
        self.assertEqual(self.service.current('FAKE.US')['review_id'], rid)

    def test_stale_new_research_is_never_replaced_by_legacy(self):
        token = self.devices.redeem(self.devices.create_pairing('synthetic'))
        actor = hashlib.sha256(token.encode()).hexdigest()
        self.service._sandbox_verifier = type('Verifier', (), {'require_current': lambda *_: None})()
        rid = self.service.create('FAKE.US', actor)['review_id']
        view = self.service.view(rid, actor)
        self.assertEqual(self.service.confirm(rid, token, view['csrf'])[0], 201)
        self.provider.revisions['FAKE.US'] = 'v2'
        self.assertEqual(self.service.current('FAKE.US')['status'], 'stale')
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'existing')
        self.assertEqual(self.service.current('FAKE.US')['status'], 'stale')

    def test_new_research_confirmed_during_preparation_wins(self):
        token = self.devices.redeem(self.devices.create_pairing('synthetic'))
        actor = hashlib.sha256(token.encode()).hexdigest()
        self.service._sandbox_verifier = type('Verifier', (), {'require_current': lambda *_: None})()
        rid = self.service.create('FAKE.US', actor)['review_id']
        view = self.service.view(rid, actor)
        original = self.provider.current
        fired = False
        def confirm_during_prepare(ticker, draft):
            nonlocal fired
            if ticker == 'FAKE.US' and not fired:
                fired = True
                self.assertEqual(self.service.confirm(rid, token, view['csrf'])[0], 201)
            return original(ticker, draft)
        self.provider.current = confirm_during_prepare
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'existing')
        self.assertEqual(self.service.current('FAKE.US')['review_id'], rid)

    def test_new_research_racing_a_stale_legacy_replacement_wins(self):
        self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                         snapshots=self.snapshots, evidence=self.evidence)
        self.provider.revisions['FAKE.US'] = 'v2'
        token = self.devices.redeem(self.devices.create_pairing('synthetic'))
        actor = hashlib.sha256(token.encode()).hexdigest()
        self.service._sandbox_verifier = type('Verifier', (), {'require_current': lambda *_: None})()
        rid = self.service.create('FAKE.US', actor)['review_id']
        view = self.service.view(rid, actor)
        original = self.provider.current
        fired = False
        def confirm_during_prepare(ticker, draft):
            nonlocal fired
            if ticker == 'FAKE.US' and not fired:
                fired = True
                self.assertEqual(self.service.confirm(rid, token, view['csrf'])[0], 201)
            return original(ticker, draft)
        self.provider.current = confirm_during_prepare
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'existing')
        self.assertEqual(self.service.current('FAKE.US')['review_id'], rid)

    def test_authorized_scope_cannot_be_silently_extended(self):
        restricted = LocalBatchAuthorization('synthetic-thread/turn-7', '同意迁移假公司',
                                             'synthetic-workline/handoff.md', ('FAKE.US',))
        batch = self.service.accept_legacy_batch(restricted, archive=self.archive, snapshots={'FAKE.US': self.snapshots['FAKE.US']}, evidence={'FAKE.US': self.evidence['FAKE.US']})
        self.assertEqual(set(batch['items']), {'FAKE.US'})
        self.assertEqual(self.service.current('NEXT.US')['status'], 'missing')
        self.assertNotIn('NEXT.US', self.provider.calculations)

    def test_archive_members_are_not_migrated(self):
        self.archive.archive('NEXT.US')
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive, snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['NEXT.US'], 'archived')
        self.assertEqual(self.service.current('NEXT.US')['status'], 'missing')
        self.assertNotIn('NEXT.US', self.provider.calculations)

    def test_archive_change_during_preparation_is_seen_at_commit(self):
        original = self.provider.current
        once = False
        def archive_while_preparing(ticker, draft):
            nonlocal once
            if ticker == 'FAKE.US' and not once:
                once = True
                self.archive.archive(ticker)
            return original(ticker, draft)
        self.provider.current = archive_while_preparing
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive, snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'archived')
        self.assertEqual(self.service.current('FAKE.US')['status'], 'missing')

    def test_archive_writes_share_batch_lock(self):
        entered = threading.Event()
        finished = threading.Event()
        def archive():
            entered.set()
            self.archive.archive('FAKE.US')
            finished.set()
        with self.archive.locked():
            thread = threading.Thread(target=archive)
            thread.start()
            self.assertTrue(entered.wait(2))
            self.assertFalse(finished.wait(0.1))
        thread.join(2)
        self.assertTrue(finished.is_set())
        self.assertIn('FAKE.US', self.archive.tickers())

    def test_archive_lock_is_shared_across_processes(self):
        context = multiprocessing.get_context('spawn')
        started = context.Event()
        finished = context.Event()
        with self.archive.locked():
            process = context.Process(target=archive_from_process,
                                      args=(str(self.root / 'archive.json'), started, finished))
            process.start()
            self.assertTrue(started.wait(5))
            self.assertFalse(finished.wait(0.1))
        process.join(5)
        self.assertEqual(process.exitcode, 0)
        self.assertTrue(finished.is_set())
        self.assertIn('FAKE.US', self.archive.tickers())

    def test_archive_lock_is_shared_through_file_symlinks(self):
        self.archive.archive('NEXT.US')
        link = self.root / 'archive-alias.json'
        link.symlink_to(self.root / 'archive.json')
        other = FrameworkArchive(link)
        started, finished = threading.Event(), threading.Event()
        def archive():
            started.set()
            other.archive('FAKE.US')
            finished.set()
        with self.archive.locked():
            thread = threading.Thread(target=archive)
            thread.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(finished.wait(0.1))
        thread.join(2)
        self.assertTrue(finished.is_set())

    def test_review_concerns_survive_batch_current_home_and_review_summary(self):
        self.provider.outputs['FAKE.US']['review_flags'] = ['single_broker_path_not_above_high_consensus']
        self.provider.outputs['FAKE.US']['option'] = {
            'status': 'requires_reconciliation', 'anchors': {'L': 10, 'M': 21, 'H': 32}}
        self.snapshots['FAKE.US'] = self.provider.snapshot('FAKE.US')
        self.write_evidence('FAKE.US')
        token = self.devices.redeem(self.devices.create_pairing('synthetic'))
        actor = hashlib.sha256(token.encode()).hexdigest()
        rid = self.service.create('FAKE.US', actor)['review_id']
        summary = self.service.view(rid, actor)['summary']
        self.assertEqual(summary['review_flags'], ['single_broker_path_not_above_high_consensus'])
        self.assertEqual(summary['option']['status'], 'requires_reconciliation')
        self.service.accept_legacy_batch(self.auth, archive=self.archive, snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(self.service.current('FAKE.US')['output']['review_flags'],
                         ['single_broker_path_not_above_high_consensus'])
        comparison = self.service.current('FAKE.US')['historical_comparison']
        self.assertEqual(comparison['review_flags']['values'], ['single_broker_path_not_above_high_consensus'])
        self.assertEqual(comparison['option_status']['value'], 'requires_reconciliation')
        row = self.row()
        self.assertEqual(row['review_flags'], ['single_broker_path_not_above_high_consensus'])
        self.assertEqual(row['option']['review_status'], 'requires_reconciliation')
        self.assertEqual(row['option_review_status'], 'requires_reconciliation')

    def test_option_status_survives_even_if_zero_value_hides_band(self):
        self.provider.outputs['FAKE.US']['option'] = {
            'status': 'requires_reconciliation', 'value': 0, 'value_unit': 'USD_PER_SHARE',
            'anchors': {'L': 10, 'M': 21, 'H': 32}}
        self.snapshots['FAKE.US'] = self.provider.snapshot('FAKE.US')
        self.write_evidence('FAKE.US')
        self.service.accept_legacy_batch(self.auth, archive=self.archive, snapshots=self.snapshots, evidence=self.evidence)
        row = self.row()
        self.assertIsNone(row['option'])
        self.assertEqual(row['option_review_status'], 'requires_reconciliation')

    def test_unavailable_extras_can_flow_through_legacy_batch_without_fake_bands(self):
        self.provider.outputs['FAKE.US']['candidate'] = {'status': 'unavailable', 'label': '扩产'}
        self.provider.outputs['FAKE.US']['option'] = {'status': 'unavailable'}
        self.snapshots['FAKE.US'] = self.provider.snapshot('FAKE.US')
        self.write_evidence('FAKE.US')
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        row = self.row()
        self.assertEqual(row['M'], 20)
        self.assertIsNone(row['candidate'])
        self.assertIsNone(row['option'])
        self.assertEqual(row['candidate_review_status'], 'unavailable')
        self.assertEqual(row['option_review_status'], 'unavailable')

    def test_unapproved_extra_snapshot_is_rejected_and_no_model_is_executed(self):
        restricted = LocalBatchAuthorization('synthetic-thread/turn-7', '同意迁移假公司',
                                             'synthetic-workline/handoff.md', ('FAKE.US',))
        with self.assertRaises(ValueError):
            self.service.accept_legacy_batch(restricted, archive=self.archive, snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(self.provider.calculations, [])
        self.assertEqual(self.service.current('FAKE.US')['status'], 'missing')

    def test_different_output_with_same_input_is_visible_as_unverified_difference(self):
        self.provider.outputs['FAKE.US']['anchors']['M'] = 25
        self.snapshots['FAKE.US'] = self.provider.snapshot('FAKE.US')
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        row = self.row()
        self.assertEqual(row['M'], 25)
        self.assertIn('historical_values_differ', row['historical_evidence_flags'])
        diff = next(item for item in row['historical_evidence_differences'] if item['field'] == 'anchors.M')
        self.assertEqual((diff['historical'], diff['current']), (20, 25))
        self.assertEqual(row['historical_evidence_source']['reference'], 'FAKE.US-old-result.json')

    def test_large_unrounded_difference_is_visible_and_never_called_consistent(self):
        output = self.provider.outputs['FAKE.US']
        output['anchors'] = {'L': 10, 'M': 200, 'H': 300}
        self.snapshots['FAKE.US'] = self.provider.snapshot('FAKE.US')
        old = json.loads(self.evidence['FAKE.US'].result_path.read_bytes())
        old['unrounded_fields'] = ['anchors.M', 'anchors.H']
        self.evidence['FAKE.US'].result_path.write_bytes(_json_bytes(old))
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        row = self.row()
        diff = next(item for item in row['historical_evidence_differences'] if item['field'] == 'anchors.M')
        self.assertEqual(diff['kind'], 'unrounded_difference')
        self.assertEqual((diff['historical'], diff['current']), (20, 200))
        self.assertEqual((diff['historical_unit'], diff['current_unit']), ('USD_PER_SHARE', 'USD_PER_SHARE'))

    def test_fund_reference_reconciliation_is_preserved_and_visible(self):
        output = self.provider.outputs['FAKE.US']
        output['fund_reference_status'] = 'requires_reconciliation'
        output['review_flags'] = ['different_date_reference']
        self.snapshots['FAKE.US'] = self.provider.snapshot('FAKE.US')
        self.write_evidence('FAKE.US')
        token = self.devices.redeem(self.devices.create_pairing('synthetic'))
        actor = hashlib.sha256(token.encode()).hexdigest()
        rid = self.service.create('FAKE.US', actor)['review_id']
        summary = self.service.view(rid, actor)['summary']
        self.assertEqual(summary['fund_reference_status'], 'requires_reconciliation')
        self.assertEqual(summary['review_flags'], ['different_date_reference'])
        self.service.accept_legacy_batch(self.auth, archive=self.archive, snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(self.row()['fund_reference_status'], 'requires_reconciliation')
        self.assertEqual(self.row()['review_flags'], ['different_date_reference'])

    def test_historical_markdown_base_band_is_valid_evidence_without_execution(self):
        doc = self.root / 'FAKE.US-old.md'
        doc.write_text('# FAKE.US — 假公司\n\n> 林奇：`stalwart` | 更新：2025-11-01\n\n'
                       '价带锚点（USD）\nL = **10.00** · M = **20.00** · H = **30.00**\n', encoding='utf-8')
        old = self.evidence['FAKE.US']
        self.evidence['FAKE.US'] = HistoricalEvidence(doc, old.script_path, '对照历史文档')
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        current = self.service.current('FAKE.US')
        self.assertEqual(current['verification'], 'historical_unverified')
        self.assertIn('anchors_unknown', current['historical_evidence_flags'])

    def test_markdown_without_currency_or_machine_shaped_anchors_remains_available(self):
        doc = self.root / 'FAKE.US-old.md'
        doc.write_text('# FAKE.US — 假公司\n\n> 林奇：`stalwart` | 更新：2025-11-01\n\n'
                       '历史正文只写三种情景，未给出机器可识别的价带。', encoding='utf-8')
        old = self.evidence['FAKE.US']
        self.evidence['FAKE.US'] = HistoricalEvidence(doc, old.script_path, '对照旧正文')
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        row = self.row()
        self.assertEqual(row['M'], 20)
        self.assertIn('currency_unknown', row['historical_evidence_flags'])
        self.assertIn('anchors_unknown', row['historical_evidence_flags'])

    def test_old_currency_from_other_context_is_preserved_and_conflict_visible(self):
        doc = self.root / 'FAKE.US-old.md'
        excerpt = '历史估值价带统一以港元计价。'
        doc.write_text('# FAKE.US — 假公司\n\n> 林奇：`stalwart` | 更新：2025-11-01\n\n'
                       '价带锚点\nL = **10.00** · M = **20.00** · H = **30.00**\n' + excerpt, encoding='utf-8')
        old = self.evidence['FAKE.US']
        self.evidence['FAKE.US'] = HistoricalEvidence(doc, old.script_path, '对照旧正文')
        self.write_manifest('FAKE.US', {'currency': {'state': 'known', 'value': 'HKD', 'scope': 'valuation_band',
                                                     'source_excerpt': excerpt, 'rationale': '旧正文价带币种'}})
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        row = self.row()
        self.assertEqual(row['M'], 20)
        diff = next(item for item in row['historical_evidence_differences'] if item['field'] == 'currency')
        self.assertEqual((diff['historical'], diff['current']), ('HKD', 'USD'))

    def test_cited_currency_manifest_resolves_otherwise_unknown_currency(self):
        doc = self.root / 'FAKE.US-old.md'
        excerpt = '附注：估值价带以美元计价。'
        doc.write_text('# FAKE.US — 假公司\n\n> 林奇：`stalwart` | 更新：2025-11-01\n\n'
                       '价带锚点\nL = **10.00** · M = **20.00** · H = **30.00**\n' + excerpt, encoding='utf-8')
        old = self.evidence['FAKE.US']
        self.evidence['FAKE.US'] = HistoricalEvidence(doc, old.script_path, '对照旧正文')
        self.write_manifest('FAKE.US', {'currency': {'state': 'known', 'value': 'USD', 'scope': 'valuation_band',
                                                     'source_excerpt': excerpt, 'rationale': '旧正文附注'}})
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        current = self.service.current('FAKE.US')
        self.assertEqual(current['historical_comparison']['currency']['value'], 'USD')
        self.assertNotIn('currency_unknown', current['historical_evidence_flags'])

    def test_currency_source_excerpt_is_an_actual_contiguous_source_line(self):
        doc = self.root / 'FAKE.US-old.md'
        text = ('# FAKE.US — 假公司\n\n> 林奇：`stalwart` | 更新：2025-11-01\n\n'
                '价带锚点\nL = **10.00** · M = **20.00** · H = **30.00**\n'
                '历史估值价带计价币种：美元。\n中间有其他说明。\n估值价带币种仍为美元。')
        doc.write_text(text, encoding='utf-8')
        old = self.evidence['FAKE.US']
        self.evidence['FAKE.US'] = HistoricalEvidence(doc, old.script_path, '对照旧正文')
        self.write_manifest('FAKE.US', {'currency': {'state': 'known', 'value': 'USD', 'scope': 'valuation_band',
                                                     'source_excerpt': '历史估值价带计价币种：美元。', 'rationale': '旧正文价带币种'}})
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        excerpt = self.service.current('FAKE.US')['historical_comparison']['currency']['source_excerpt']
        self.assertIn(excerpt, text)

    def test_business_revenue_currency_does_not_establish_valuation_currency(self):
        doc = self.root / 'FAKE.US-old.md'
        excerpt = '海外云业务收入以美元计价。'
        doc.write_text('# FAKE.US — 假公司\n\n> 林奇：`stalwart` | 更新：2025-11-01\n\n'
                       '价带锚点\nL = **10.00** · M = **20.00** · H = **30.00**\n' + excerpt, encoding='utf-8')
        old = self.evidence['FAKE.US']
        self.evidence['FAKE.US'] = HistoricalEvidence(doc, old.script_path, '对照旧正文')
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        current = self.service.current('FAKE.US')
        self.assertEqual(current['historical_comparison']['currency']['state'], 'unknown')
        self.assertIn('currency_unknown', current['historical_evidence_flags'])
        app = create_app(self.docs, self.root / 'no-dist', Quotes(), self.devices, self.archive,
                         valuation_provider=self.service)
        with TestClient(app, base_url='http://testserver') as client:
            self.assertEqual(client.post('/api/pair', json={'code': self.devices.create_pairing('http')}).status_code, 204)
            row = client.get('/api/tickers').json()['tickers'][0]
            self.assertEqual(row['M'], 20)
            self.assertIn('currency_unknown', row['historical_evidence_flags'])

    def test_revenue_currency_on_anchor_line_is_not_a_valuation_currency(self):
        doc = self.root / 'FAKE.US-old.md'
        doc.write_text('# FAKE.US — 假公司\n\n> 林奇：`stalwart` | 更新：2025-11-01\n\n'
                       '价带锚点：海外云业务收入以美元计价。\n'
                       'L = **10.00** · M = **20.00** · H = **30.00**\n', encoding='utf-8')
        old = self.evidence['FAKE.US']
        self.evidence['FAKE.US'] = HistoricalEvidence(doc, old.script_path, '对照旧正文')
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        self.assertIn('currency_unknown', self.service.current('FAKE.US')['historical_evidence_flags'])

    def test_operator_cannot_infer_valuation_currency_from_revenue_citation(self):
        doc = self.root / 'FAKE.US-old.md'
        excerpt = '海外云业务收入以美元计价。'
        doc.write_text('# FAKE.US — 假公司\n\n> 林奇：`stalwart` | 更新：2025-11-01\n\n'
                       '价带锚点\nL = **10.00** · M = **20.00** · H = **30.00**\n' + excerpt, encoding='utf-8')
        old = self.evidence['FAKE.US']
        self.evidence['FAKE.US'] = HistoricalEvidence(doc, old.script_path, '对照旧正文')
        self.write_manifest('FAKE.US', {'currency': {'state': 'known', 'value': 'USD',
                                                     'source_excerpt': excerpt, 'rationale': '仅为收入币种'}})
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        self.assertIn('currency_unknown', self.service.current('FAKE.US')['historical_evidence_flags'])

    def test_incomplete_historical_candidate_keeps_name_and_script_band(self):
        self.provider.outputs['FAKE.US']['candidate'] = {'label': '扩产', 'anchors': {'L': 11, 'M': 22, 'H': 33}}
        self.snapshots['FAKE.US'] = self.provider.snapshot('FAKE.US')
        old = json.loads(self.evidence['FAKE.US'].result_path.read_bytes())
        old['candidate'] = {'label': '扩产'}
        self.evidence['FAKE.US'].result_path.write_bytes(_json_bytes(old))
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        row = self.row()
        self.assertEqual(row['candidate']['M'], 22)
        self.assertIn('candidate_anchors_unknown', row['historical_evidence_flags'])
        self.assertEqual(self.service.current('FAKE.US')['historical_comparison']['candidate']['data']['label'], '扩产')

    def test_partially_known_historical_candidate_tiers_do_not_disappear(self):
        self.provider.outputs['FAKE.US']['candidate'] = {'label': '扩产', 'anchors': {'L': 11, 'M': 22, 'H': 33}}
        self.snapshots['FAKE.US'] = self.provider.snapshot('FAKE.US')
        old = json.loads(self.evidence['FAKE.US'].result_path.read_bytes())
        old['candidate'] = {'anchors': {'L': 11, 'M': None, 'H': 33}}
        self.evidence['FAKE.US'].result_path.write_bytes(_json_bytes(old))
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        current = self.service.current('FAKE.US')
        self.assertIn('candidate_label_unknown', current['historical_evidence_flags'])
        self.assertIn('candidate_anchors_unknown', current['historical_evidence_flags'])
        self.assertEqual(current['historical_comparison']['candidate']['data']['anchors'], {'L': 11, 'H': 33})

    def test_nonstructured_old_candidate_and_unit_are_unknown_not_a_blocker(self):
        old = json.loads(self.evidence['FAKE.US'].result_path.read_bytes())
        old['candidate'] = '旧材料仅有口语描述，具体档位未整理'
        old['units'] = '币种口径需核对'
        self.evidence['FAKE.US'].result_path.write_bytes(_json_bytes(old))
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        row = self.row()
        self.assertEqual(row['M'], 20)
        self.assertIn('candidate_unknown', row['historical_evidence_flags'])
        self.assertIn('anchors_unit_unknown', row['historical_evidence_flags'])

    def test_structured_history_with_missing_base_and_currency_keeps_script_result(self):
        old = json.loads(self.evidence['FAKE.US'].result_path.read_bytes())
        del old['anchors']
        del old['currency']
        del old['units']
        self.evidence['FAKE.US'].result_path.write_bytes(_json_bytes(old))
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        row = self.row()
        self.assertEqual(row['M'], 20)
        self.assertIn('currency_unknown', row['historical_evidence_flags'])
        self.assertIn('anchors_unknown', row['historical_evidence_flags'])

    def test_known_old_candidate_is_exposed_as_reference_without_current_band(self):
        old = json.loads(self.evidence['FAKE.US'].result_path.read_bytes())
        old['candidate'] = {'label': '扩产', 'anchors': {'L': 11, 'M': 22, 'H': 33}}
        self.evidence['FAKE.US'].result_path.write_bytes(_json_bytes(old))
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        row = self.row()
        self.assertIsNone(row['candidate'])
        historical = row['historical_reference_bands'][0]
        self.assertEqual((historical['kind'], historical['label']), ('candidate', '扩产'))
        self.assertEqual(historical['anchors'], {'L': 11, 'M': 22, 'H': 33})
        self.assertEqual(historical['unit'], 'USD_PER_SHARE')
        self.assertEqual(row['historical_evidence_source']['reference'], 'FAKE.US-old-result.json')

    def test_real_home_route_returns_historical_reference_not_current_candidate(self):
        old = json.loads(self.evidence['FAKE.US'].result_path.read_bytes())
        old['candidate'] = {'label': '扩产', 'anchors': {'L': 11, 'M': 22, 'H': 33}}
        self.evidence['FAKE.US'].result_path.write_bytes(_json_bytes(old))
        self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                         snapshots=self.snapshots, evidence=self.evidence)
        app = create_app(self.docs, self.root / 'no-dist', Quotes(), self.devices, self.archive,
                         valuation_provider=self.service)
        with TestClient(app, base_url='http://testserver') as client:
            self.assertEqual(client.post('/api/pair', json={'code': self.devices.create_pairing('http')}).status_code, 204)
            row = client.get('/api/tickers').json()['tickers'][0]
            self.assertIsNone(row['candidate'])
            self.assertEqual(row['historical_reference_bands'][0]['anchors'], {'L': 11, 'M': 22, 'H': 33})
            self.assertEqual(row['historical_reference_bands'][0]['unit'], 'USD_PER_SHARE')

    def test_old_unavailable_option_with_prices_is_reported_as_internal_conflict(self):
        self.provider.outputs['FAKE.US']['option'] = {'status': 'unavailable'}
        self.snapshots['FAKE.US'] = self.provider.snapshot('FAKE.US')
        old = json.loads(self.evidence['FAKE.US'].result_path.read_bytes())
        old['option'] = {'status': 'unavailable', 'anchors': {'L': 11, 'M': 22, 'H': 33}}
        self.evidence['FAKE.US'].result_path.write_bytes(_json_bytes(old))
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        row = self.row()
        self.assertEqual(row['M'], 20)
        self.assertIn('historical_option_internally_inconsistent', row['historical_evidence_flags'])
        self.assertEqual(row['historical_reference_bands'][0]['anchors'], {'L': 11, 'M': 22, 'H': 33})
        self.assertTrue(any(d['kind'] == 'historical_internal_conflict' for d in row['historical_evidence_differences']))

    def test_old_unavailable_candidate_with_prices_is_visible_as_source_conflict(self):
        self.provider.outputs['FAKE.US']['candidate'] = {'status': 'unavailable', 'label': '扩产'}
        self.snapshots['FAKE.US'] = self.provider.snapshot('FAKE.US')
        old = json.loads(self.evidence['FAKE.US'].result_path.read_bytes())
        old['candidate'] = {'status': 'unavailable', 'label': '扩产', 'anchors': {'L': 11, 'M': 22, 'H': 33}}
        self.evidence['FAKE.US'].result_path.write_bytes(_json_bytes(old))
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        row = self.row()
        self.assertEqual(row['M'], 20)
        self.assertIsNone(row['candidate'])
        self.assertEqual(row['candidate_review_status'], 'unavailable')
        self.assertIn('historical_candidate_internally_inconsistent', row['historical_evidence_flags'])
        reference = row['historical_reference_bands'][0]
        self.assertEqual((reference['kind'], reference['label'], reference['status']), ('candidate', '扩产', 'unavailable'))
        self.assertEqual(reference['anchors'], {'L': 11, 'M': 22, 'H': 33})
        self.assertTrue(any(d['field'] == 'candidate.source_consistency' and d['kind'] == 'historical_internal_conflict'
                            for d in row['historical_evidence_differences']))
        app = create_app(self.docs, self.root / 'no-dist', Quotes(), self.devices, self.archive,
                         valuation_provider=self.service)
        with TestClient(app, base_url='http://testserver') as client:
            self.assertEqual(client.post('/api/pair', json={'code': self.devices.create_pairing('http')}).status_code, 204)
            served = client.get('/api/tickers').json()['tickers'][0]
            self.assertIsNone(served['candidate'])
            self.assertEqual(served['historical_reference_bands'][0]['anchors'], {'L': 11, 'M': 22, 'H': 33})

    def test_current_read_never_reextracts_valuation_from_historical_markdown(self):
        doc = self.root / 'FAKE.US-old.md'
        doc.write_text('# FAKE.US — 假公司\n\n> 林奇：`stalwart` | 更新：2025-11-01\n\n'
                       '价带锚点（USD）\nL = **10.00** · M = **20.00** · H = **30.00**\n', encoding='utf-8')
        old = self.evidence['FAKE.US']
        self.evidence['FAKE.US'] = HistoricalEvidence(doc, old.script_path, '对照历史文档')
        self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                         snapshots=self.snapshots, evidence=self.evidence)
        with patch('lynch_reader.legacy_batches._historical_matches', side_effect=AssertionError('unexpected reparse')):
            self.assertEqual(self.service.current('FAKE.US')['output']['anchors']['M'], 20)
            self.assertEqual(self.row()['M'], 20)

    def test_structured_historical_evidence_checks_candidate_and_option(self):
        output = self.provider.outputs['FAKE.US']
        output['candidate'] = {'label': '扩产', 'anchors': {'L': 11, 'M': 22, 'H': 33}}
        output['option'] = {'value': 3, 'value_unit': 'USD_PER_SHARE', 'anchors': {'L': 10, 'M': 22, 'H': 33}}
        self.snapshots['FAKE.US'] = self.provider.snapshot('FAKE.US')
        self.write_evidence('FAKE.US')
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        self.assertEqual(self.row()['candidate']['label'], '扩产')
        self.assertEqual(self.row()['option']['v_opt'], 3)

    def test_structured_historical_evidence_exposes_changed_candidate_or_option(self):
        for group in ('candidate', 'option'):
            with self.subTest(group=group):
                self.service = ReviewService(self.root / (group + '-different.sqlite'), self.provider, self.devices)
                output = self.provider.outputs['FAKE.US']
                output['candidate'] = {'label': '扩产', 'anchors': {'L': 11, 'M': 22, 'H': 33}}
                output['option'] = {'value': 3, 'value_unit': 'USD_PER_SHARE', 'anchors': {'L': 10, 'M': 22, 'H': 33}}
                self.write_evidence('FAKE.US')
                output[group]['anchors']['M'] = 23
                self.snapshots['FAKE.US'] = self.provider.snapshot('FAKE.US')
                batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                         snapshots=self.snapshots, evidence=self.evidence)
                diff = self.assert_visible_difference(batch, group + '.anchors.M')
                self.assertEqual((diff['historical'], diff['current']), (22, 23))

    def test_markdown_evidence_does_not_silently_approve_unchecked_candidate(self):
        self.provider.outputs['FAKE.US']['candidate'] = {'label': '候选', 'anchors': {'L': 11, 'M': 22, 'H': 33}}
        self.snapshots['FAKE.US'] = self.provider.snapshot('FAKE.US')
        doc = self.root / 'FAKE.US-old.md'
        doc.write_text('# FAKE.US — 假公司\n\n> 林奇：`stalwart` | 更新：2025-11-01\n\n'
                       '价带锚点（USD）\nL = **10.00** · M = **20.00** · H = **30.00**\n', encoding='utf-8')
        old = self.evidence['FAKE.US']
        self.evidence['FAKE.US'] = HistoricalEvidence(doc, old.script_path, '对照历史文档')
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        self.assertIn('candidate_unknown', self.service.current('FAKE.US')['historical_evidence_flags'])

    def test_markdown_cannot_silently_drop_historical_candidate(self):
        doc = self.root / 'FAKE.US-old.md'
        doc.write_text('# FAKE.US — 假公司\n\n> 林奇：`stalwart` | 更新：2025-11-01\n\n'
                       '价带锚点（USD）\nL = **10.00** · M = **20.00** · H = **30.00**\n'
                       '- 候选带（新产能）：L′ = **11** · M′ = **22** · H′ = **33**\n', encoding='utf-8')
        old = self.evidence['FAKE.US']
        self.evidence['FAKE.US'] = HistoricalEvidence(doc, old.script_path, '对照历史文档')
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        self.assertIn('candidate_unknown', self.row()['historical_evidence_flags'])

    def test_historical_evidence_changed_before_commit_keeps_result_unreviewed(self):
        original = self.provider.current
        count = 0
        def edit_evidence_at_commit(ticker, draft):
            nonlocal count
            if ticker == 'FAKE.US':
                count += 1
                if count == 2:
                    self.evidence[ticker].result_path.write_text('{}', encoding='utf-8')
            return original(ticker, draft)
        self.provider.current = edit_evidence_at_commit
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        self.assertEqual(self.service.current('FAKE.US')['status'], 'legacy_accepted')
        self.assertIn('historical_source_changed', self.row()['historical_evidence_flags'])

    def test_missing_historical_evidence_keeps_script_result_marked_for_review(self):
        evidence = {'NEXT.US': self.evidence['NEXT.US']}
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        self.assertEqual(batch['items']['NEXT.US'], 'accepted')
        self.assertEqual(self.row()['M'], 20)
        self.assertIn('historical_material_unreviewed', self.row()['historical_evidence_flags'])

    def test_cross_line_material_never_parses_prose_or_interrupts_other_tickers(self):
        doc = self.root / 'FAKE.US-crossline.md'
        doc.write_text('# FAKE.US — 假公司\n\n价带锚点\n（美元）收入增长但未注明估值币种。\n'
                       'L = **10.00** · M = **20.00** · H = **30.00**\n', encoding='utf-8')
        old = self.evidence['FAKE.US']
        self.evidence['FAKE.US'] = HistoricalEvidence(doc, old.script_path, '旧资料待核')
        with patch('eval.lib.frameworks._parse_anchors', side_effect=AssertionError('unexpected old parser')) as old_parser:
            batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                     snapshots=self.snapshots, evidence=self.evidence)
            old_parser.assert_not_called()
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        self.assertEqual(batch['items']['NEXT.US'], 'accepted')
        current = self.service.current('FAKE.US')
        self.assertEqual(current['historical_comparison']['currency']['state'], 'unknown')
        self.assertIn('historical_material_unreviewed', current['historical_evidence_flags'])
        self.assertEqual(self.row()['M'], 20)

    def test_operator_can_supply_cited_structured_historical_base_without_parsing_prose(self):
        doc = self.root / 'FAKE.US-unstructured.md'
        excerpt = '旧研究的三档是 10、20、30。'
        doc.write_text('# 假公司旧研究\n\n' + excerpt, encoding='utf-8')
        old = self.evidence['FAKE.US']
        self.evidence['FAKE.US'] = HistoricalEvidence(doc, old.script_path, '旧研究')
        self.write_manifest('FAKE.US', {'anchors': {'state': 'known', 'data': {'L': 10, 'M': 20, 'H': 30},
                            'source_excerpt': excerpt, 'rationale': '按旧研究整理'}})
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        current = self.service.current('FAKE.US')
        self.assertNotIn('anchors_unknown', current['historical_evidence_flags'])
        self.assertEqual(current['historical_comparison']['anchors']['source'], 'operator_attested')
        self.assertEqual(current['historical_comparison']['anchors']['source_excerpt'], excerpt)

    def test_uncitable_manifest_does_not_block_script_or_other_tickers(self):
        self.write_manifest('FAKE.US', {'candidate': {'state': 'absent',
                            'source_excerpt': '原材料没有这句话', 'rationale': '待核'}})
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        self.assertEqual(batch['items']['NEXT.US'], 'accepted')
        self.assertIn('historical_material_unreviewed', self.row()['historical_evidence_flags'])
        with self.service._db() as con:
            raw = con.execute('SELECT manifest FROM legacy_batches WHERE id=?', (batch['batch_id'],)).fetchone()[0]
        frozen = json.loads(raw)['items']['FAKE.US']['historical_evidence']
        self.assertEqual(base64.b64decode(frozen['result_bytes']), self.evidence['FAKE.US'].result_path.read_bytes())
        self.assertEqual(base64.b64decode(frozen['manifest_bytes']), self.evidence['FAKE.US'].manifest_path.read_bytes())
        self.assertEqual(frozen['verification'], 'historical_unverified')

    def test_one_provider_exception_does_not_interrupt_the_other_ticker(self):
        original = self.provider.current
        def fail_one(ticker, draft):
            if ticker == 'FAKE.US':
                raise RuntimeError('synthetic provider error')
            return original(ticker, draft)
        self.provider.current = fail_one
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        self.assertEqual(self.row()['M'], 20)
        self.assertIn('valuation_source_unavailable', self.row()['historical_evidence_flags'])
        self.assertEqual(batch['items']['NEXT.US'], 'accepted')

    def test_historical_script_from_another_ticker_keeps_result_unreviewed(self):
        old = self.evidence['FAKE.US']
        self.evidence['FAKE.US'] = HistoricalEvidence(old.result_path, self.evidence['NEXT.US'].script_path, old.rationale)
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        self.assertEqual(self.row()['M'], 20)
        self.assertIn('historical_material_unreviewed', self.row()['historical_evidence_flags'])

    def test_stale_legacy_result_can_be_replaced_but_preserves_prior_event(self):
        first = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        with self.service._db() as con:
            old_version = con.execute("SELECT version FROM results WHERE ticker='FAKE.US'").fetchone()[0]
        self.provider.revisions['FAKE.US'] = 'v2'
        self.assertEqual(self.service.current('FAKE.US')['status'], 'legacy_accepted')
        self.assertIn('valuation_source_changed', self.service.current('FAKE.US')['historical_evidence_flags'])
        self.provider.outputs['FAKE.US']['anchors']['M'] = 21
        self.snapshots['FAKE.US'] = self.provider.snapshot('FAKE.US')
        self.write_evidence('FAKE.US')
        second = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                  snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(second['items']['FAKE.US'], 'replaced')
        self.assertEqual(self.service.current('FAKE.US')['output']['anchors']['M'], 21)
        with self.service._db() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM legacy_items WHERE ticker='FAKE.US'").fetchone()[0], 2)
            manifest = json.loads(con.execute('SELECT manifest FROM legacy_batches WHERE id=?',
                                              (second['batch_id'],)).fetchone()[0])
            self.assertEqual(manifest['items']['FAKE.US']['replaces_version'], old_version)
        self.assertNotEqual(first['batch_id'], second['batch_id'])

    def test_unrelated_operator_attestation_cannot_replace_stale_legacy(self):
        self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                         snapshots=self.snapshots, evidence=self.evidence)
        self.provider.revisions['FAKE.US'] = 'v2'
        other = LocalBatchAuthorization('different-thread', '另一次迁移授权',
                                        'another-workline/handoff.md', self.auth.scope)
        batch = self.service.accept_legacy_batch(other, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'authorization_mismatch')
        self.assertEqual(self.service.current('FAKE.US')['status'], 'legacy_accepted')
        self.assertIn('valuation_source_changed', self.service.current('FAKE.US')['historical_evidence_flags'])

    def test_structured_history_conflicting_units_are_not_consistent(self):
        old = json.loads(self.evidence['FAKE.US'].result_path.read_bytes())
        old['units']['price'] = 'USD_PER_100_SHARES'
        self.evidence['FAKE.US'].result_path.write_bytes(_json_bytes(old))
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        diff = self.assert_visible_difference(batch, 'anchors.unit')
        self.assertEqual((diff['historical'], diff['current']), ('USD_PER_100_SHARES', 'USD_PER_SHARE'))

    def test_structured_history_currency_metadata_cannot_contradict_its_ticker_currency(self):
        old = json.loads(self.evidence['FAKE.US'].result_path.read_bytes())
        old['units']['currency'] = 'EUR'
        self.evidence['FAKE.US'].result_path.write_bytes(_json_bytes(old))
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        diff = self.assert_visible_difference(batch, 'currency.metadata')
        self.assertEqual((diff['historical'], diff['current']), ('EUR', 'USD'))

    def test_known_historical_flags_and_option_status_cannot_disappear(self):
        output = self.provider.outputs['FAKE.US']
        output['option'] = {'status': 'conditional', 'anchors': {'L': 10, 'M': 21, 'H': 32}}
        output['review_flags'] = []
        self.snapshots['FAKE.US'] = self.provider.snapshot('FAKE.US')
        self.write_evidence('FAKE.US')
        old = json.loads(self.evidence['FAKE.US'].result_path.read_bytes())
        old['review_flags'] = ['historic_risk']
        old['option']['status'] = 'requires_reconciliation'
        self.evidence['FAKE.US'].result_path.write_bytes(_json_bytes(old))
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assert_visible_difference(batch, 'review_flags')
        self.assert_visible_difference(batch, 'option.status')
        row = self.row()
        self.assertEqual(row['historical_review_flags'], ['historic_risk'])
        self.assertEqual(row['historical_option_status'], 'requires_reconciliation')

    def test_known_option_status_cannot_be_downgraded_independently(self):
        output = self.provider.outputs['FAKE.US']
        output['option'] = {'status': 'conditional', 'anchors': {'L': 10, 'M': 21, 'H': 32}}
        output['review_flags'] = ['historic_risk']
        self.snapshots['FAKE.US'] = self.provider.snapshot('FAKE.US')
        self.write_evidence('FAKE.US')
        old = json.loads(self.evidence['FAKE.US'].result_path.read_bytes())
        old['option']['status'] = 'requires_reconciliation'
        self.evidence['FAKE.US'].result_path.write_bytes(_json_bytes(old))
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        diff = self.assert_visible_difference(batch, 'option.status')
        self.assertEqual((diff['historical'], diff['current']), ('requires_reconciliation', 'conditional'))

    def test_manifest_known_option_status_cannot_be_dropped_with_option(self):
        old = json.loads(self.evidence['FAKE.US'].result_path.read_bytes())
        old['legacy_status_note'] = 'requires_reconciliation'
        self.evidence['FAKE.US'].result_path.write_bytes(_json_bytes(old))
        self.write_manifest('FAKE.US', {'option_status': {'state': 'known', 'value': 'requires_reconciliation',
                            'source_excerpt': 'requires_reconciliation', 'rationale': '旧资料明确待核'}})
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        diff = self.assert_visible_difference(batch, 'option.status')
        self.assertEqual((diff['historical'], diff['current']), ('requires_reconciliation', None))

    def test_unusable_precision_claim_keeps_result_unreviewed(self):
        old = json.loads(self.evidence['FAKE.US'].result_path.read_bytes())
        old['display_precision'] = {'option.value': 2}
        self.evidence['FAKE.US'].result_path.write_bytes(_json_bytes(old))
        self.write_manifest('FAKE.US', {'precision': {'option.value': {'mode': 'displayed', 'decimals': 2,
                            'source_excerpt': '"option":null', 'precision_source_excerpt': '"option.value":2',
                            'rationale': '并无可对照期权'}}})
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        self.assertIn('historical_material_unreviewed', self.row()['historical_evidence_flags'])

    def test_known_review_flags_cannot_be_dropped_independently(self):
        old = json.loads(self.evidence['FAKE.US'].result_path.read_bytes())
        old['review_flags'] = ['historic_risk']
        self.evidence['FAKE.US'].result_path.write_bytes(_json_bytes(old))
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        diff = self.assert_visible_difference(batch, 'review_flags')
        self.assertEqual((diff['historical'], diff['current']), (['historic_risk'], []))
        self.assertEqual(self.row()['historical_review_flags'], ['historic_risk'])

    def test_operator_can_preserve_known_flag_and_add_a_cited_disclosure(self):
        self.provider.outputs['FAKE.US']['review_flags'] = ['historic_risk', 'single_broker']
        self.snapshots['FAKE.US'] = self.provider.snapshot('FAKE.US')
        self.write_evidence('FAKE.US')
        old = json.loads(self.evidence['FAKE.US'].result_path.read_bytes())
        old['review_flags'] = ['historic_risk']
        old['legacy_note'] = '预测仅来自一家卖方'
        self.evidence['FAKE.US'].result_path.write_bytes(_json_bytes(old))
        self.write_manifest('FAKE.US', {'review_flags': {'state': 'known',
                            'values': ['historic_risk', 'single_broker'], 'source_excerpt': '预测仅来自一家卖方',
                            'rationale': '保留原风险并补充旧材料中已存在的单一卖方提示'}})
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        self.assertEqual(self.row()['review_flags'], ['historic_risk', 'single_broker'])

    def test_missing_historical_units_remain_explicitly_unknown(self):
        old = json.loads(self.evidence['FAKE.US'].result_path.read_bytes())
        del old['units']
        self.evidence['FAKE.US'].result_path.write_bytes(_json_bytes(old))
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        current = self.service.current('FAKE.US')
        self.assertIn('anchors_unit_unknown', current['historical_evidence_flags'])
        self.assertEqual(current['historical_comparison']['price_units']['anchors']['state'], 'unknown')

    def test_candidate_and_option_unit_conflicts_are_not_silenced(self):
        output = self.provider.outputs['FAKE.US']
        output['candidate'] = {'label': '扩产', 'anchors': {'L': 11, 'M': 22, 'H': 33}}
        output['option'] = {'anchors': {'L': 10, 'M': 21, 'H': 32}}
        self.snapshots['FAKE.US'] = self.provider.snapshot('FAKE.US')
        self.write_evidence('FAKE.US')
        for part, wrong in (('candidate', 'EUR_PER_SHARE'), ('option', 'USD_PER_FUND_UNIT')):
            with self.subTest(part=part):
                self.service = ReviewService(self.root / (part + '-unit-diff.sqlite'), self.provider, self.devices)
                old = json.loads(self.evidence['FAKE.US'].result_path.read_bytes())
                old['candidate'].pop('unit', None)
                old['option'].pop('unit', None)
                old[part]['unit'] = wrong
                self.evidence['FAKE.US'].result_path.write_bytes(_json_bytes(old))
                batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                         snapshots=self.snapshots, evidence=self.evidence)
                diff = self.assert_visible_difference(batch, part + '.unit')
                self.assertIn(wrong, diff['historical'])

    def test_matching_total_option_amount_is_not_misrepresented_as_per_share(self):
        output = self.provider.outputs['FAKE.US']
        output['option'] = {'value': 100, 'value_unit': 'USD_MILLION',
                            'anchors': {'L': 10, 'M': 21, 'H': 32}}
        self.snapshots['FAKE.US'] = self.provider.snapshot('FAKE.US')
        self.write_evidence('FAKE.US')
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        self.assertIn('option_value_unit_incompatible', self.service.current('FAKE.US')['historical_evidence_flags'])
        self.assertNotIn('v_opt', self.row()['option'])

    def test_new_option_value_not_in_historical_source_is_marked_unknown(self):
        output = self.provider.outputs['FAKE.US']
        output['option'] = {'value': 2, 'value_unit': 'USD_PER_SHARE',
                            'anchors': {'L': 10, 'M': 21, 'H': 32}}
        self.snapshots['FAKE.US'] = self.provider.snapshot('FAKE.US')
        self.write_evidence('FAKE.US')
        old = json.loads(self.evidence['FAKE.US'].result_path.read_bytes())
        del old['option']['value']
        self.evidence['FAKE.US'].result_path.write_bytes(_json_bytes(old))
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        self.assertIn('option_value_unknown', self.service.current('FAKE.US')['historical_evidence_flags'])

    def test_known_option_value_unit_cannot_be_changed_when_amount_missing(self):
        output = self.provider.outputs['FAKE.US']
        output['option'] = {'value_unit': 'USD_PER_SHARE', 'anchors': {'L': 10, 'M': 21, 'H': 32}}
        self.snapshots['FAKE.US'] = self.provider.snapshot('FAKE.US')
        self.write_evidence('FAKE.US')
        old = json.loads(self.evidence['FAKE.US'].result_path.read_bytes())
        old['option']['value_unit'] = 'EUR_PER_SHARE'
        self.evidence['FAKE.US'].result_path.write_bytes(_json_bytes(old))
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        diff = self.assert_visible_difference(batch, 'option.value_unit')
        self.assertEqual((diff['historical'], diff['current']), ('EUR_PER_SHARE', 'USD_PER_SHARE'))

    def test_unrecognized_colloquial_candidate_is_unknown_not_absent(self):
        doc = self.root / 'FAKE.US-old.md'
        doc.write_text('# FAKE.US — 假公司\n\n> 林奇：`stalwart` | 更新：2025-11-01\n\n'
                       '价带锚点（USD）\nL = **10.00** · M = **20.00** · H = **30.00**\n'
                       '候选情景：扩产达成时，三个档位为 11 / 22 / 33 美元。\n', encoding='utf-8')
        old = self.evidence['FAKE.US']
        self.evidence['FAKE.US'] = HistoricalEvidence(doc, old.script_path, '对照历史文档')
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        self.assertIn('candidate_unknown', self.service.current('FAKE.US')['historical_evidence_flags'])
        self.assertIn('candidate_unknown', self.row()['historical_evidence_flags'])

    def test_actual_home_route_exposes_unknown_history_without_reading_old_prices(self):
        doc = self.root / 'FAKE.US-old.md'
        doc.write_text('# FAKE.US — 假公司\n\n> 林奇：`stalwart` | 更新：2025-11-01\n\n'
                       '价带锚点（USD）\nL = **10.00** · M = **20.00** · H = **30.00**\n'
                       '候选情景：扩产达成时，三个档位为 11 / 22 / 33 美元。\n', encoding='utf-8')
        old = self.evidence['FAKE.US']
        self.evidence['FAKE.US'] = HistoricalEvidence(doc, old.script_path, '对照历史文档')
        app = create_app(self.docs, self.root / 'no-dist', Quotes(), self.devices, self.archive,
                         valuation_provider=self.service)
        with TestClient(app, base_url='http://testserver') as client:
            self.assertEqual(client.post('/api/pair', json={'code': self.devices.create_pairing('http')}).status_code, 204)
            first = client.get('/api/tickers')
            etag = first.headers['etag']
            self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                              snapshots=self.snapshots, evidence=self.evidence)
            # No formal read may reparse the historical Markdown after import.
            with patch('lynch_reader.historical_comparison.compare', side_effect=AssertionError('unexpected reparse')):
                changed = client.get('/api/tickers', headers={'If-None-Match': etag})
            self.assertEqual(changed.status_code, 200)
            row = changed.json()['tickers'][0]
            self.assertEqual(row['valuation_status'], 'legacy_accepted')
            self.assertEqual(row['M'], 20)
            self.assertIn('candidate_unknown', row['historical_evidence_flags'])
            self.assertEqual(row['historical_evidence_states']['candidate'], 'unknown')

    def test_operator_manifest_records_known_colloquial_candidate_and_its_source(self):
        doc = self.root / 'FAKE.US-old.md'
        excerpt = '候选情景：扩产达成时，三个档位为 11 / 22 / 33 美元。'
        doc.write_text('# FAKE.US — 假公司\n\n> 林奇：`stalwart` | 更新：2025-11-01\n\n'
                       '价带锚点（USD）\nL = **10.00** · M = **20.00** · H = **30.00**\n' + excerpt, encoding='utf-8')
        output = self.provider.outputs['FAKE.US']
        output['candidate'] = {'label': '扩产', 'anchors': {'L': 11, 'M': 22, 'H': 33}}
        self.snapshots['FAKE.US'] = self.provider.snapshot('FAKE.US')
        old = self.evidence['FAKE.US']
        self.evidence['FAKE.US'] = HistoricalEvidence(doc, old.script_path, '对照历史文档')
        self.write_manifest('FAKE.US', {'candidate': {'state': 'present', 'source_excerpt': excerpt,
                                                      'rationale': '原文三档', 'data': output['candidate']}})
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        self.assertEqual(self.row()['candidate']['label'], '扩产')
        self.assertNotIn('candidate_unknown', self.service.current('FAKE.US')['historical_evidence_flags'])

    def test_operator_manifest_preserves_known_prose_risk_and_mapping_source(self):
        doc = self.root / 'FAKE.US-old.md'
        excerpt = '旧预测仅来自一家卖方，仍需核对。'
        doc.write_text('# FAKE.US — 假公司\n\n> 林奇：`stalwart` | 更新：2025-11-01\n\n'
                       '价带锚点（USD）\nL = **10.00** · M = **20.00** · H = **30.00**\n' + excerpt, encoding='utf-8')
        self.provider.outputs['FAKE.US']['review_flags'] = ['single_broker_forecast']
        self.snapshots['FAKE.US'] = self.provider.snapshot('FAKE.US')
        old = self.evidence['FAKE.US']
        self.evidence['FAKE.US'] = HistoricalEvidence(doc, old.script_path, '对照历史文档')
        self.write_manifest('FAKE.US', {'review_flags': {'state': 'known', 'values': ['single_broker_forecast'],
                            'source_excerpt': excerpt, 'rationale': '将单一卖方风险映射为稳定标识'}})
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        current = self.service.current('FAKE.US')
        detail = current['historical_comparison']['review_flags']
        self.assertEqual(detail['source'], 'operator_attested')
        self.assertEqual(detail['source_excerpt'], excerpt)
        self.assertEqual(detail['values'], ['single_broker_forecast'])
        self.assertNotIn('review_flags_unknown', current['historical_evidence_flags'])

    def test_operator_manifest_preserves_known_option_status_with_unknown_old_band(self):
        doc = self.root / 'FAKE.US-old.md'
        excerpt = '新业务的潜在价值尚需核对。'
        doc.write_text('# FAKE.US — 假公司\n\n> 林奇：`stalwart` | 更新：2025-11-01\n\n'
                       '价带锚点（USD）\nL = **10.00** · M = **20.00** · H = **30.00**\n' + excerpt, encoding='utf-8')
        self.provider.outputs['FAKE.US']['option'] = {
            'status': 'requires_reconciliation', 'anchors': {'L': 10, 'M': 21, 'H': 32}}
        self.snapshots['FAKE.US'] = self.provider.snapshot('FAKE.US')
        old = self.evidence['FAKE.US']
        self.evidence['FAKE.US'] = HistoricalEvidence(doc, old.script_path, '对照历史文档')
        self.write_manifest('FAKE.US', {'option_status': {'state': 'known', 'value': 'requires_reconciliation',
                            'source_excerpt': excerpt, 'rationale': '原文明确说明待核'}})
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        current = self.service.current('FAKE.US')
        self.assertEqual(current['historical_comparison']['option_status']['source'], 'operator_attested')
        self.assertIn('option_unknown', current['historical_evidence_flags'])
        self.assertEqual(self.row()['option_review_status'], 'requires_reconciliation')

    def test_operator_manifest_records_explicit_absence_without_faking_completeness(self):
        doc = self.root / 'FAKE.US-old.md'
        excerpt = '旧框架明确没有候选带。'
        doc.write_text('# FAKE.US — 假公司\n\n> 林奇：`stalwart` | 更新：2025-11-01\n\n'
                       '价带锚点（USD）\nL = **10.00** · M = **20.00** · H = **30.00**\n' + excerpt, encoding='utf-8')
        old = self.evidence['FAKE.US']
        self.evidence['FAKE.US'] = HistoricalEvidence(doc, old.script_path, '对照历史文档')
        self.write_manifest('FAKE.US', {'candidate': {'state': 'absent', 'source_excerpt': excerpt,
                                                      'rationale': '正文明确说明'}})
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        current = self.service.current('FAKE.US')
        self.assertEqual(current['historical_comparison']['candidate']['state'], 'absent')
        self.assertEqual(current['historical_comparison']['candidate']['source_excerpt'], excerpt)
        self.assertEqual(self.row()['historical_evidence_states']['candidate'], 'absent')
        self.assertNotIn('candidate_unknown', current['historical_evidence_flags'])
        self.assertIn('candidate_absence_attested', current['historical_evidence_flags'])
        self.assertIn('option_unknown', current['historical_evidence_flags'])

    def test_operator_manifest_cannot_hide_a_cited_candidate_unit_conflict(self):
        doc = self.root / 'FAKE.US-old.md'
        excerpt = '候选扩产 L 11 M 22 H 33 EUR_PER_SHARE'
        doc.write_text('# FAKE.US — 假公司\n\n> 林奇：`stalwart` | 更新：2025-11-01\n\n'
                       '价带锚点（USD）\nL = **10.00** · M = **20.00** · H = **30.00**\n' + excerpt, encoding='utf-8')
        output = self.provider.outputs['FAKE.US']
        output['candidate'] = {'label': '扩产', 'anchors': {'L': 11, 'M': 22, 'H': 33}}
        self.snapshots['FAKE.US'] = self.provider.snapshot('FAKE.US')
        old = self.evidence['FAKE.US']
        self.evidence['FAKE.US'] = HistoricalEvidence(doc, old.script_path, '对照历史文档')
        historical_candidate = {**output['candidate'], 'unit': 'EUR_PER_SHARE'}
        self.write_manifest('FAKE.US', {'candidate': {'state': 'present', 'source_excerpt': excerpt,
                                                      'rationale': '原文明确单位', 'data': historical_candidate}})
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        diff = self.assert_visible_difference(batch, 'candidate.unit')
        self.assertEqual((diff['historical'], diff['current']), ('EUR_PER_SHARE', 'USD_PER_SHARE'))

    def test_operator_transcription_retains_source_without_machine_claim_of_verification(self):
        doc = self.root / 'FAKE.US-old.md'
        excerpt = '候选扩产 L 11 M 33 H 22 美元'
        doc.write_text('# FAKE.US — 假公司\n\n> 林奇：`stalwart` | 更新：2025-11-01\n\n'
                       '价带锚点（USD）\nL = **10.00** · M = **20.00** · H = **30.00**\n' + excerpt, encoding='utf-8')
        output = self.provider.outputs['FAKE.US']
        output['candidate'] = {'label': '扩产', 'anchors': {'L': 11, 'M': 22, 'H': 33}}
        self.snapshots['FAKE.US'] = self.provider.snapshot('FAKE.US')
        old = self.evidence['FAKE.US']
        self.evidence['FAKE.US'] = HistoricalEvidence(doc, old.script_path, '对照历史文档')
        self.write_manifest('FAKE.US', {'candidate': {'state': 'present', 'source_excerpt': excerpt,
                                                      'rationale': '原文三档', 'data': output['candidate']}})
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        current = self.service.current('FAKE.US')
        self.assertIn('historical_material_unreviewed', current['historical_evidence_flags'])
        self.assertEqual(current['historical_comparison']['candidate']['source_excerpt'], excerpt)

    def test_operator_large_integer_transcription_is_not_claimed_as_verified(self):
        doc = self.root / 'FAKE.US-old.md'
        excerpt = '候选扩产 L 9007199254740993 M 9007199254740994 H 9007199254740996'
        doc.write_text('# FAKE.US — 假公司\n\n> 林奇：`stalwart` | 更新：2025-11-01\n\n'
                       '价带锚点（USD）\nL = **10.00** · M = **20.00** · H = **30.00**\n' + excerpt, encoding='utf-8')
        output = self.provider.outputs['FAKE.US']
        output['candidate'] = {'label': '扩产', 'anchors': {'L': 9007199254740992,
                                                        'M': 9007199254740994, 'H': 9007199254740996}}
        self.snapshots['FAKE.US'] = self.provider.snapshot('FAKE.US')
        old = self.evidence['FAKE.US']
        self.evidence['FAKE.US'] = HistoricalEvidence(doc, old.script_path, '对照历史文档')
        self.write_manifest('FAKE.US', {'candidate': {'state': 'present', 'source_excerpt': excerpt,
                                                      'rationale': '原文三档', 'data': output['candidate']}})
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        current = self.service.current('FAKE.US')
        self.assertIn('historical_material_unreviewed', current['historical_evidence_flags'])
        self.assertEqual(current['historical_comparison']['candidate']['source_excerpt'], excerpt)

    def test_absence_manifest_conflicts_with_a_new_candidate(self):
        doc = self.root / 'FAKE.US-old.md'
        excerpt = '旧框架明确没有候选带。'
        doc.write_text('# FAKE.US — 假公司\n\n> 林奇：`stalwart` | 更新：2025-11-01\n\n'
                       '价带锚点（USD）\nL = **10.00** · M = **20.00** · H = **30.00**\n' + excerpt, encoding='utf-8')
        output = self.provider.outputs['FAKE.US']
        output['candidate'] = {'label': '扩产', 'anchors': {'L': 11, 'M': 22, 'H': 33}}
        self.snapshots['FAKE.US'] = self.provider.snapshot('FAKE.US')
        old = self.evidence['FAKE.US']
        self.evidence['FAKE.US'] = HistoricalEvidence(doc, old.script_path, '对照历史文档')
        self.write_manifest('FAKE.US', {'candidate': {'state': 'absent', 'source_excerpt': excerpt,
                                                      'rationale': '正文明确说明'}})
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        diff = self.assert_visible_difference(batch, 'candidate.presence')
        self.assertEqual((diff['historical'], diff['current']), ('absent', 'present'))

    def test_operator_manifest_with_missing_citation_keeps_result_unreviewed(self):
        self.write_manifest('FAKE.US', {'candidate': {'state': 'absent', 'source_excerpt': '正文不存在这段话',
                                                      'rationale': '不存在的出处'}})
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        self.assertIn('historical_material_unreviewed', self.row()['historical_evidence_flags'])

    def test_only_explicit_displayed_precision_allows_rounded_json_value(self):
        output = self.provider.outputs['FAKE.US']
        output['anchors']['M'] = 20.234
        self.snapshots['FAKE.US'] = self.provider.snapshot('FAKE.US')
        old = json.loads(self.evidence['FAKE.US'].result_path.read_bytes())
        old['anchors']['M'] = 20.23
        old['display_precision'] = {'anchors.M': 2}
        self.evidence['FAKE.US'].result_path.write_bytes(_json_bytes(old))
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        diff = self.assert_visible_difference(batch, 'anchors.M')
        self.assertEqual(diff['kind'], 'precision_unknown_difference')
        self.provider.revisions['FAKE.US'] = 'v2'
        self.snapshots['FAKE.US'] = self.provider.snapshot('FAKE.US')
        self.write_manifest('FAKE.US', {'precision': {'anchors.M': {'mode': 'displayed', 'decimals': 2,
                            'source_excerpt': '"M":20.23', 'precision_source_excerpt': '"anchors.M":2',
                            'rationale': '原记录注明只显示两位'}}})
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'replaced')
        self.assertIn('display_precision_attested', self.service.current('FAKE.US')['historical_evidence_flags'])
        self.assertFalse(any(item['field'] == 'anchors.M' for item in self.row()['historical_evidence_differences']))

    def test_truncated_precision_citation_keeps_result_unreviewed(self):
        output = self.provider.outputs['FAKE.US']
        output['anchors']['M'] = 20.4
        self.snapshots['FAKE.US'] = self.provider.snapshot('FAKE.US')
        old = json.loads(self.evidence['FAKE.US'].result_path.read_bytes())
        old['anchors']['M'] = 20.23
        old['display_precision'] = {'anchors.M': 0}
        self.evidence['FAKE.US'].result_path.write_bytes(_json_bytes(old))
        self.write_manifest('FAKE.US', {'precision': {'anchors.M': {'mode': 'displayed', 'decimals': 0,
                            'source_excerpt': '"M":20', 'precision_source_excerpt': '"anchors.M":0',
                            'rationale': '只引用了数值的一部分'}}})
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        self.assertIn('historical_material_unreviewed', self.row()['historical_evidence_flags'])

    def test_operator_precision_claim_cannot_override_explicit_unrounded_field(self):
        output = self.provider.outputs['FAKE.US']
        output['anchors']['M'] = 20.234
        self.snapshots['FAKE.US'] = self.provider.snapshot('FAKE.US')
        old = json.loads(self.evidence['FAKE.US'].result_path.read_bytes())
        old['anchors']['M'] = 20.23
        old['unrounded_fields'] = ['anchors.M']
        self.evidence['FAKE.US'].result_path.write_bytes(_json_bytes(old))
        self.write_manifest('FAKE.US', {'precision': {'anchors.M': {'mode': 'displayed', 'decimals': 2,
                            'source_excerpt': '"M":20.23', 'precision_source_excerpt': '"M":20.23',
                            'rationale': '没有实际精度声明'}}})
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        current = self.service.current('FAKE.US')
        self.assertIn('precision_source_conflict', current['historical_evidence_flags'])
        self.assertTrue(any(item['kind'] == 'unrounded_difference' for item in current['historical_evidence_differences']))

    def test_unknown_rounding_rule_cannot_approve_an_ambiguous_halfway_value(self):
        output = self.provider.outputs['FAKE.US']
        output['anchors']['M'] = 20.225
        self.snapshots['FAKE.US'] = self.provider.snapshot('FAKE.US')
        old = json.loads(self.evidence['FAKE.US'].result_path.read_bytes())
        old['anchors']['M'] = 20.23
        old['display_precision'] = {'anchors.M': 2}
        self.evidence['FAKE.US'].result_path.write_bytes(_json_bytes(old))
        self.write_manifest('FAKE.US', {'precision': {'anchors.M': {'mode': 'displayed', 'decimals': 2,
                            'source_excerpt': '"M":20.23', 'precision_source_excerpt': '"anchors.M":2',
                            'rationale': '原记录仅注明两位，未注明舍入方式'}}})
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        diff = self.assert_visible_difference(batch, 'anchors.M')
        self.assertEqual(diff['kind'], 'display_difference')

    def test_markdown_option_value_uses_its_recorded_display_precision(self):
        doc = self.root / 'FAKE.US-old.md'
        excerpt = '- \\(V_{opt}\\) = **2.3**'
        doc.write_text('# FAKE.US — 假公司\n\n> 林奇：`stalwart` | 更新：2025-11-01\n\n'
                       '价带锚点（USD）\nL = **10.00** · M = **20.00** · H = **30.00**\n'
                       + excerpt + '\n', encoding='utf-8')
        output = self.provider.outputs['FAKE.US']
        output['option'] = {'value': 2.34, 'value_unit': 'USD_PER_SHARE',
                            'anchors': {'L': 10, 'M': 21, 'H': 32}}
        self.snapshots['FAKE.US'] = self.provider.snapshot('FAKE.US')
        old = self.evidence['FAKE.US']
        self.evidence['FAKE.US'] = HistoricalEvidence(doc, old.script_path, '对照历史文档')
        self.write_manifest('FAKE.US', {'option': {'state': 'present', 'data': {'value': 2.3},
                            'source_excerpt': excerpt, 'rationale': '原文列示新业务价值'},
                            'precision': {'option.value': {'mode': 'displayed', 'decimals': 1,
                            'source_excerpt': excerpt, 'rationale': '原文显示一位小数'}}})
        batch = self.service.accept_legacy_batch(self.auth, archive=self.archive,
                                                 snapshots=self.snapshots, evidence=self.evidence)
        self.assertEqual(batch['items']['FAKE.US'], 'accepted')
        comparison = self.service.current('FAKE.US')['historical_comparison']
        self.assertEqual(comparison['precision']['option.value']['decimals'], 1)
        self.assertIn('option_anchors_unknown', comparison['uncertainties'])


if __name__ == '__main__':
    unittest.main()
