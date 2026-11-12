"""Fictional data only; no stock calculation or sandbox execution occurs."""
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
from threading import Event
import unittest

from eval.lib.result_view import map_result
from eval.lib.runner_draft import draft_from_result
from eval.lib.valuation_drafts import Draft, RevisionConflict, _json_bytes, _load_json
from eval.lib.valuation_runner import _external_digest
from lynch_reader.archive import FrameworkArchive
from lynch_reader.conversation_publication import ConversationAuthorization, prepare, publish
from lynch_reader.sandbox_valuation import SandboxConfig, SandboxProvider
from lynch_reader.valuation_reviews import ReviewService
WORKSPACE = Path(os.environ.get('LYNCH_TEST_CODE_ROOT', Path(__file__).resolve().parents[3]))


class ConversationPublicationTest(unittest.TestCase):
    def setUp(self):
        temp_root = Path.cwd() / '_local/tmp'
        temp_root.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(prefix='conversation-publication-', dir=temp_root)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.code, self.content = self.root / 'code', self.root / 'content'
        self.sources = {'runner': self.code / 'eval/lib/valuation_runner.py',
                        'lib_init': self.code / 'eval/lib/__init__.py',
                        'stock': self.content / 'eval/FAKE.US.py',
                        'axis:market': self.content / 'axes/market.py'}
        contents = {'runner': b'# fictional runner\n', 'lib_init': b'',
                    'stock': b'DEPENDENCIES = {"libraries": (), "axes": {"market": "market"}}\n',
                    'axis:market': b'# fictional axis\n'}
        for key, path in self.sources.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents[key])
        self.external = {'history': [1, 2]}
        self.output = {'ticker': 'FAKE.US', 'currency': 'USD', 'valuation_as_of': None,
                       'framework_as_of': None, 'anchors': {'L': 1, 'M': 2, 'H': 3},
                       'candidate': None, 'option': None, 'basis_changed': None,
                       'partial_basis_changed': False, 'confirmation_status': 'unconfirmed',
                       'source_versions': {key: hashlib.sha256(path.read_bytes()).hexdigest()
                                           for key, path in self.sources.items()},
                       'source_paths': {key: {'selected': str(path), 'resolved': str(path)}
                                        for key, path in self.sources.items()},
                       'root_identity': {'code_selected': str(self.code), 'code_resolved': str(self.code),
                                         'content_selected': str(self.content), 'content_resolved': str(self.content)},
                       'external_inputs_digest': _external_digest(self.external), 'axis_metadata': {},
                       'peer_snapshots': {}, 'peer_result_digests': {}, 'peer_missing': [],
                       'runtime': {'executable': 'fictional', 'environment_bound': False}}
        self.external_file = self.root / 'history.json'
        self.external_file.write_bytes(_json_bytes(self.external))
        config = SandboxConfig(self.code, self.content, None, self.root, self.root / 'registry',
                               {'FAKE.US': 'USD'}, external_sources={'FAKE.US': self.external_file})
        self.provider = SandboxProvider(config, read_only=True)
        self.service = ReviewService(self.root / 'results.sqlite', self.provider, None)
        self.archive_file = self.code / 'output/archive/frameworks.json'
        self.archive = FrameworkArchive(self.archive_file)
        self.draft = draft_from_result(self.output, external_inputs=self.external)
        self.authorization = ConversationAuthorization('fake-session/turn-2', '可以落盘')

    def publish_it(self, draft, receipt, authorization=None):
        return publish(self.service, self.archive, draft, receipt,
                       self.authorization if authorization is None else authorization,
                       code_root=self.provider.code, content_root=self.provider.content)

    def cli_deployment(self):
        shutil.copytree(WORKSPACE / 'eval/lib', self.code / 'eval/lib', dirs_exist_ok=True)
        shutil.copytree(WORKSPACE / 'app/server/lynch_reader', self.code / 'app/server/lynch_reader')
        (self.code / 'frameworks').symlink_to(self.content, target_is_directory=True)
        (self.code / 'app/server/.venv').symlink_to(Path(sys.executable).parent.parent, target_is_directory=True)
        for key in ('runner', 'lib_init'):
            self.output['source_versions'][key] = hashlib.sha256(self.sources[key].read_bytes()).hexdigest()
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONPATH='app/server',
                   LYNCH_VALUATION_DB=str(self.service.path))
        source_config = self.root / 'fictional-source-config.json'
        source_config.write_bytes(_json_bytes({'code_root': str(self.code), 'content_root': str(self.content),
            'tickers': {'FAKE.US': 'USD'}, 'external_sources': {'FAKE.US': str(self.external_file)}}))
        env['LYNCH_VALUATION_SOURCES'] = str(source_config)
        return env

    def test_requires_explicit_existing_user_authorization(self):
        receipt = prepare(self.service, self.draft)
        with self.assertRaises(ValueError):
            publish(self.service, self.archive, self.draft, receipt, None,
                    code_root=self.provider.code, content_root=self.provider.content)
        self.assertEqual(self.service.current('FAKE.US')['status'], 'missing')
        with self.assertRaises(ValueError):
            ConversationAuthorization('fake-session/turn-2', '')

    def test_approved_result_updates_same_store_without_claiming_device_confirmation(self):
        receipt = prepare(self.service, self.draft)
        self.assertEqual(self.service.current('FAKE.US')['status'], 'missing')
        saved = self.publish_it(self.draft, receipt)
        self.assertIsInstance(saved, dict)
        self.assertEqual(saved['status'], 'local_accepted')
        current = self.service.current('FAKE.US')
        self.assertEqual(current['status'], 'local_accepted')
        self.assertEqual(current['output']['anchors']['M'], 2)
        self.assertEqual(current['origin'], 'local_operator_attestation')
        self.assertNotIn('snapshot_confirmed', current)
        state, view = map_result('FAKE.US', current)
        self.assertEqual(state, 'local_accepted')
        self.assertEqual(view.M, 2)

    def test_different_output_or_changed_source_does_not_replace_previous(self):
        self.publish_it(self.draft, prepare(self.service, self.draft))
        preview = prepare(self.service, self.draft)
        modified = _load_json(self.draft._document)
        modified['output']['anchors']['M'] = 2.5
        altered = Draft(_json_bytes(modified))
        with self.assertRaises(ValueError):
            self.publish_it(altered, preview)
        self.sources['axis:market'].write_text('# changed after user saw proposal', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.publish_it(self.draft, preview)
        self.assertEqual(self.service.current('FAKE.US')['output']['anchors']['M'], 2)

    def test_external_source_change_does_not_replace_previous_even_when_runtime_unavailable(self):
        self.publish_it(self.draft, prepare(self.service, self.draft))
        receipt = prepare(self.service, self.draft)
        self.external_file.write_bytes(_json_bytes({'history': [99]}))
        conditions = self.provider.source_conditions('FAKE.US', self.draft)
        self.assertIn('external_history_source_changed', conditions)
        self.assertIn('valuation_runtime_unavailable', conditions)
        with self.assertRaisesRegex(ValueError, 'source changed'):
            self.publish_it(self.draft, receipt)
        self.assertEqual(self.service.current('FAKE.US')['output']['anchors']['M'], 2)

    def test_simultaneous_updates_cannot_overwrite_one_another(self):
        receipt_a = prepare(self.service, self.draft)
        receipt_b = prepare(self.service, self.draft)
        def run(receipt):
            try:
                self.publish_it(self.draft, receipt)
                return 'ok'
            except RevisionConflict:
                return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(run, (receipt_a, receipt_b)))
        self.assertCountEqual(outcomes, ['ok', 'conflict'])
        self.assertEqual(self.service.current('FAKE.US')['output']['anchors']['M'], 2)

    def test_finalized_paths_can_differ_from_proposal_including_peer_sources(self):
        from eval.lib.valuation_runner import _json_digest
        stock = self.sources['stock']
        stock.write_text('DEPENDENCIES = {"libraries": (), "axes": {"market": "market"}, '
                         '"peers": {"peer": "OTHER.US"}}\n', encoding='utf-8')
        self.output['source_versions']['stock'] = hashlib.sha256(stock.read_bytes()).hexdigest()
        peer_stock = self.content / 'eval/OTHER.US.py'
        peer_stock.write_text('DEPENDENCIES = {"libraries": (), "axes": {"market": "market"}}\n')
        peer = copy.deepcopy(self.output)
        peer['ticker'] = 'OTHER.US'
        peer['source_paths']['stock'] = {'selected': str(peer_stock), 'resolved': str(peer_stock)}
        peer['source_versions']['stock'] = hashlib.sha256(peer_stock.read_bytes()).hexdigest()
        peer['external_inputs_digest'] = None
        self.output['peer_snapshots'] = {'peer': peer}
        self.output['peer_result_digests'] = {'peer': _json_digest(peer)}
        proposal = draft_from_result(self.output, external_inputs=self.external)
        receipt = prepare(self.service, proposal)
        final_code, final_content = self.root / 'final-code', self.root / 'final-content'
        shutil.copytree(self.code, final_code)
        shutil.copytree(self.content, final_content)
        final = copy.deepcopy(self.output)
        for result in (final, final['peer_snapshots']['peer']):
            for key, record in result['source_paths'].items():
                content = key == 'stock' or key.startswith('axis:')
                prior, dest = (self.content, final_content) if content else (self.code, final_code)
                path = dest / Path(record['selected']).relative_to(prior)
                record.update(selected=str(path), resolved=str(path))
            result['root_identity'] = {'code_selected': str(final_code), 'code_resolved': str(final_code),
                                       'content_selected': str(final_content), 'content_resolved': str(final_content)}
        final['peer_result_digests'] = {'peer': _json_digest(final['peer_snapshots']['peer'])}
        self.provider.code = self.provider.code_selected = final_code
        self.provider.content = self.provider.content_selected = final_content
        saved = self.publish_it(draft_from_result(final, external_inputs=self.external), receipt)
        self.assertEqual(saved['status'], 'local_accepted')

    def test_temporary_result_cannot_be_published_as_the_formal_worktree(self):
        receipt = prepare(self.service, self.draft)
        formal_code = self.root / 'formal-code'
        formal_content = self.root / 'formal-content'
        shutil.copytree(self.code, formal_code)
        shutil.copytree(self.content, formal_content)
        self.provider.code = formal_code
        self.provider.content = formal_content
        try:
            with self.assertRaisesRegex(ValueError, 'formal'):
                self.publish_it(self.draft, receipt)
        except TypeError as exc:
            self.fail(f'formal source roots are not yet supported: {exc}')
        self.assertEqual(self.service.current('FAKE.US')['status'], 'missing')

    def test_archived_and_invalid_snapshot_do_not_replace_previous(self):
        self.publish_it(self.draft, prepare(self.service, self.draft))
        receipt = prepare(self.service, self.draft)
        self.archive.archive('FAKE.US')
        with self.assertRaises(ValueError):
            self.publish_it(self.draft, receipt)
        self.assertEqual(self.service.current('FAKE.US')['output']['anchors']['M'], 2)
        self.archive.restore('FAKE.US')
        broken = _load_json(self.draft._document)
        broken['inputs']['content/axes/market.py'] = base64.b64encode(b'tampered').decode()
        with self.assertRaises(ValueError):
            self.publish_it(Draft(_json_bytes(broken)), receipt)
        self.assertEqual(self.service.current('FAKE.US')['output']['anchors']['M'], 2)

    def test_agent_entry_publishes_only_after_explicit_authorization(self):
        env = self.cli_deployment()
        result_file = self.root / 'runner-result.json'
        result_file.write_bytes(_json_bytes(self.output))
        archive_file = self.archive_file
        receipt_file = self.root / 'receipt.json'
        command = [str(self.code / 'app/server/.venv/bin/python'), '-m', 'lynch_reader.conversation_publication']
        shared = ['--database', str(self.service.path), '--result', str(result_file),
                  '--external-inputs', str(self.external_file)]
        prepared = subprocess.run(command + ['prepare'] + shared, capture_output=True, text=True, env=env, cwd=self.code)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        receipt_file.write_text(prepared.stdout, encoding='utf-8')
        self.assertEqual(self.service.current('FAKE.US')['status'], 'missing')
        args = ['publish'] + shared + ['--archive', str(archive_file), '--receipt', str(receipt_file),
                                     '--session-reference', 'fake-session/turn-2',
                                     '--code-root', str(self.code), '--content-root', str(self.content)]
        no_auth = subprocess.run(command + args, capture_output=True, text=True, env=env, cwd=self.code)
        self.assertNotEqual(no_auth.returncode, 0)
        self.assertEqual(self.service.current('FAKE.US')['status'], 'missing')
        published = subprocess.run(command + args + ['--authorization', '可以落盘'], capture_output=True, text=True, env=env, cwd=self.code)
        self.assertEqual(published.returncode, 0, published.stderr)
        self.assertEqual(json.loads(published.stdout)['status'], 'local_accepted')
        self.assertEqual(self.service.current('FAKE.US')['output']['anchors']['M'], 2)

    def test_cli_rejects_wrong_archive_without_modifying_any_store(self):
        env = self.cli_deployment()
        self.archive.archive('FAKE.US')
        result_file, receipt = self.root / 'result.json', self.root / 'receipt.json'
        result_file.write_bytes(_json_bytes(self.output))
        cmd = [str(self.code / 'app/server/.venv/bin/python'), '-m', 'lynch_reader.conversation_publication']
        common = ['--database', str(self.service.path), '--result', str(result_file),
                  '--external-inputs', str(self.external_file)]
        prepared = subprocess.run(cmd + ['prepare'] + common, env=env, capture_output=True, text=True, cwd=self.code)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        receipt.write_text(prepared.stdout, encoding='utf-8')
        wrong = self.root / 'missing-archive.json'
        args = ['publish'] + common + ['--archive', str(wrong), '--receipt', str(receipt),
                '--session-reference', 'fake-session/turn-2', '--authorization', '可以落盘',
                '--code-root', str(self.code), '--content-root', str(self.content)]
        published = subprocess.run(cmd + args, env=env, capture_output=True, text=True, cwd=self.code)
        self.assertNotEqual(published.returncode, 0, published.stdout)
        self.assertIn('archive', published.stderr.lower())
        self.assertFalse(wrong.exists())
        self.assertEqual(self.service.current('FAKE.US')['status'], 'missing')

    def test_cli_rejects_wrong_database_and_receipt_database_switch(self):
        env = self.cli_deployment()
        result_file, receipt = self.root / 'result.json', self.root / 'receipt.json'
        result_file.write_bytes(_json_bytes(self.output))
        cmd = [str(self.code / 'app/server/.venv/bin/python'), '-m', 'lynch_reader.conversation_publication']
        common = ['--result', str(result_file), '--external-inputs', str(self.external_file)]
        prepared = subprocess.run(cmd + ['prepare', '--database', str(self.service.path)] + common,
                                  env=env, capture_output=True, text=True, cwd=self.code)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        receipt.write_text(prepared.stdout, encoding='utf-8')
        wrong = self.root / 'other-results.sqlite'
        args = ['publish', '--database', str(wrong)] + common + ['--archive', str(self.archive_file),
                '--receipt', str(receipt), '--session-reference', 'fake-session/turn-2',
                '--authorization', '可以落盘', '--code-root', str(self.code), '--content-root', str(self.content)]
        result = subprocess.run(cmd + args, env=env, capture_output=True, text=True, cwd=self.code)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(wrong.exists())
        env['LYNCH_VALUATION_DB'] = str(wrong)
        switched = subprocess.run(cmd + args, env=env, capture_output=True, text=True, cwd=self.code)
        self.assertNotEqual(switched.returncode, 0, switched.stdout)
        self.assertFalse(wrong.exists())
        self.assertEqual(self.service.current('FAKE.US')['status'], 'missing')

    def test_cli_requires_database_configuration_before_prepare(self):
        env = self.cli_deployment()
        env.pop('LYNCH_VALUATION_DB', None)
        result_file = self.root / 'result.json'
        result_file.write_bytes(_json_bytes(self.output))
        cmd = [str(self.code / 'app/server/.venv/bin/python'), '-m', 'lynch_reader.conversation_publication']
        result = subprocess.run(cmd + ['prepare', '--database', str(self.service.path), '--result', str(result_file),
                                       '--external-inputs', str(self.external_file)],
                                env=env, capture_output=True, text=True, cwd=self.code)
        self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_first_use_archived_does_not_create_database(self):
        env = self.cli_deployment()
        self.archive.archive('FAKE.US')
        result = self.root / 'runner-result.json'
        result.write_bytes(_json_bytes(self.output))
        database = self.root / 'new' / 'results.sqlite'
        env['LYNCH_VALUATION_DB'] = str(database)
        receipt = self.root / 'receipt.json'
        command = [str(self.code / 'app/server/.venv/bin/python'), '-m', 'lynch_reader.conversation_publication']
        common = ['--database', str(database), '--result', str(result), '--external-inputs', str(self.external_file)]
        prepared = subprocess.run(command + ['prepare'] + common, env=env, capture_output=True, text=True, cwd=self.code)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        receipt.write_text(prepared.stdout, encoding='utf-8')
        args = ['publish'] + common + ['--archive', str(self.archive_file), '--receipt', str(receipt),
                '--session-reference', 'fake-session/turn-2', '--authorization', '可以落盘',
                '--code-root', str(self.code), '--content-root', str(self.content)]
        published = subprocess.run(command + args, env=env, capture_output=True, text=True, cwd=self.code)
        self.assertNotEqual(published.returncode, 0, published.stdout)
        self.assertFalse(database.exists(), 'rejected publication created the database')

    def test_first_use_source_change_does_not_create_database(self):
        database = self.root / 'new' / 'results.sqlite'
        service = ReviewService.open_readonly(database, self.provider)
        receipt = prepare(service, self.draft)
        self.external_file.write_bytes(_json_bytes({'history': [99]}))
        with self.assertRaisesRegex(ValueError, 'source changed'):
            publish(service, self.archive, self.draft, receipt, self.authorization,
                    code_root=self.provider.code, content_root=self.provider.content)
        self.assertFalse(database.exists())

    def test_missing_store_with_prior_version_does_not_create_database(self):
        database = self.root / 'new' / 'results.sqlite'
        service = ReviewService.open_readonly(database, self.provider)
        receipt = prepare(service, self.draft)
        receipt['expected_version'] = 'prior-version'
        with self.assertRaises(RevisionConflict):
            publish(service, self.archive, self.draft, receipt, self.authorization,
                    code_root=self.provider.code, content_root=self.provider.content)
        self.assertFalse(database.exists())

    def test_first_use_observes_archive_change_under_same_lock(self):
        database = self.root / 'new' / 'results.sqlite'
        service = ReviewService.open_readonly(database, self.provider)
        receipt = prepare(service, self.draft)
        started = Event()

        def attempt():
            started.set()
            return publish(service, self.archive, self.draft, receipt, self.authorization,
                           code_root=self.provider.code, content_root=self.provider.content)

        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.archive.locked():
                future = pool.submit(attempt)
                self.assertTrue(started.wait(1))
                with self.assertRaises(FutureTimeoutError):
                    future.result(timeout=.1)
                self.archive._write({'FAKE.US'})
            with self.assertRaisesRegex(ValueError, 'archived'):
                future.result(timeout=2)
        self.assertFalse(database.exists())

    def test_simultaneous_first_use_publications_keep_one_result(self):
        database = self.root / 'new' / 'results.sqlite'
        services = [ReviewService.open_readonly(database, self.provider) for _ in range(2)]
        receipts = [prepare(service, self.draft) for service in services]

        def attempt(pair):
            service, receipt = pair
            try:
                return publish(service, self.archive, self.draft, receipt, self.authorization,
                               code_root=self.provider.code, content_root=self.provider.content)['status']
            except RevisionConflict:
                return 'conflict'

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, zip(services, receipts)))
        self.assertCountEqual(outcomes, ['local_accepted', 'conflict'])
        self.assertEqual(ReviewService.open_readonly(database, self.provider).current('FAKE.US')['output']['anchors']['M'], 2)

    def test_first_use_creates_database_only_after_authorized_publication(self):
        env = self.cli_deployment()
        result_file = self.root / 'runner-result.json'
        result_file.write_bytes(_json_bytes(self.output))
        database = self.root / 'new' / 'results.sqlite'
        env['LYNCH_VALUATION_DB'] = str(database)
        receipt = self.root / 'receipt.json'
        command = [str(self.code / 'app/server/.venv/bin/python'), '-m', 'lynch_reader.conversation_publication']
        common = ['--database', str(database), '--result', str(result_file),
                  '--external-inputs', str(self.external_file)]
        prepared = subprocess.run(command + ['prepare'] + common, env=env, capture_output=True, text=True, cwd=self.code)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        receipt.write_text(prepared.stdout, encoding='utf-8')
        self.assertFalse(database.exists())
        args = ['publish'] + common + ['--archive', str(self.archive_file), '--receipt', str(receipt),
                '--session-reference', 'fake-session/turn-2', '--code-root', str(self.code),
                '--content-root', str(self.content)]
        empty = subprocess.run(command + args + ['--authorization', '  '], env=env, capture_output=True, text=True, cwd=self.code)
        self.assertNotEqual(empty.returncode, 0)
        self.assertFalse(database.exists())
        accepted = subprocess.run(command + args + ['--authorization', '可以落盘'], env=env, capture_output=True, text=True, cwd=self.code)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertTrue(database.is_file())

    def test_cli_rejects_temporary_roots_even_if_source_configuration_is_missing(self):
        env = self.cli_deployment()
        env.pop('LYNCH_VALUATION_SOURCES', None)
        temporary_code, temporary_content = self.root / 'temporary-code', self.root / 'temporary-content'
        shutil.copytree(self.code / 'eval', temporary_code / 'eval')
        shutil.copytree(self.content, temporary_content)
        temp_output = copy.deepcopy(self.output)
        for key, record in temp_output['source_paths'].items():
            content = key == 'stock' or key.startswith('axis:')
            prior, destination = (self.content, temporary_content) if content else (self.code, temporary_code)
            path = destination / Path(record['selected']).relative_to(prior)
            record.update(selected=str(path), resolved=str(path))
        temp_output['root_identity'] = {'code_selected': str(temporary_code), 'code_resolved': str(temporary_code),
                                        'content_selected': str(temporary_content), 'content_resolved': str(temporary_content)}
        result_file, receipt = self.root / 'temporary.json', self.root / 'receipt.json'
        result_file.write_bytes(_json_bytes(temp_output))
        database = self.root / 'new' / 'results.sqlite'
        env['LYNCH_VALUATION_DB'] = str(database)
        command = [str(self.code / 'app/server/.venv/bin/python'), '-m', 'lynch_reader.conversation_publication']
        common = ['--database', str(database), '--result', str(result_file), '--external-inputs', str(self.external_file)]
        prepared = subprocess.run(command + ['prepare'] + common, env=env, capture_output=True, text=True, cwd=self.code)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        receipt.write_text(prepared.stdout, encoding='utf-8')
        args = ['publish'] + common + ['--archive', str(self.archive_file), '--receipt', str(receipt),
                '--session-reference', 'fake-session/turn-2', '--authorization', '可以落盘',
                '--code-root', str(self.code), '--content-root', str(self.content)]
        result = subprocess.run(command + args, env=env, capture_output=True, text=True, cwd=self.code)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(database.exists())

    def test_existing_non_database_file_is_never_overwritten(self):
        env = self.cli_deployment()
        result_file, receipt = self.root / 'result.json', self.root / 'receipt.json'
        result_file.write_bytes(_json_bytes(self.output))
        command = [str(self.code / 'app/server/.venv/bin/python'), '-m', 'lynch_reader.conversation_publication']
        common = ['--result', str(result_file), '--external-inputs', str(self.external_file)]
        prepared = subprocess.run(command + ['prepare', '--database', str(self.service.path)] + common,
                                  env=env, capture_output=True, text=True, cwd=self.code)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        receipt.write_text(prepared.stdout, encoding='utf-8')
        invalid = self.root / 'not-a-database'
        invalid.write_bytes(b'fictional user data')
        env['LYNCH_VALUATION_DB'] = str(invalid)
        args = ['publish', '--database', str(invalid)] + common + ['--archive', str(self.archive_file),
            '--receipt', str(receipt), '--session-reference', 'fake/turn', '--authorization', '可以落盘',
            '--code-root', str(self.code), '--content-root', str(self.content)]
        result = subprocess.run(command + args, env=env, capture_output=True, text=True, cwd=self.code)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(invalid.read_bytes(), b'fictional user data')


if __name__ == '__main__':
    unittest.main()
