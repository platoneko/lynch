import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from eval.lib.test_research_store import metadata


class CLITest(unittest.TestCase):
    def setUp(self):
        self.repo = Path(__file__).resolve().parents[2]
        self.tmp = tempfile.TemporaryDirectory(dir=self.repo / '_local/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = self.root / 'store'
        self.meta = self.root / 'meta.json'
        self.meta.write_text(json.dumps(metadata()))
        self.source = self.root / 'alpha_cf.json'
        self.source.write_text('{"cash_oper":"123.00","capex":"-45.00"}')

    def cli(self, *args, configured=True):
        env = dict(os.environ)
        env.pop('LYNCH_RESEARCH_DIR', None)
        if configured:
            env['LYNCH_RESEARCH_DIR'] = str(self.store)
        proc = subprocess.run([sys.executable, '-m', 'eval.lib.research_cli', *map(str, args)],
                              env=env, cwd=self.repo, capture_output=True, text=True)
        return proc.returncode, json.loads(proc.stdout)

    def put(self):
        code, result = self.cli('put', '--file', self.source, '--metadata', self.meta)
        self.assertEqual(code, 0, result)
        return result

    def test_missing_config_does_not_create_a_default_store(self):
        code, result = self.cli('find', '--symbol', 'ALPHA.US', configured=False)
        self.assertEqual(code, 2)
        self.assertEqual(result['code'], 'configuration')
        self.assertFalse(self.store.exists())

    def test_put_find_and_read_preserve_original_data_and_source(self):
        saved = self.put()
        self.source.unlink()
        code, found = self.cli('find', '--symbol', 'ALPHA.US', '--query', 'cash_oper')
        self.assertEqual(code, 0)
        self.assertEqual(found['items'][0]['material_id'], saved['material_id'])
        code, read = self.cli('read', saved['material_id'])
        self.assertEqual(code, 0)
        self.assertIn('123.00', read['text'])
        self.assertEqual(read['source_ref'], 'https://example.org/report')

    def test_query_finds_match_after_twentieth_candidate_and_has_continuation(self):
        from eval.lib.research_store import archive_file
        from eval.lib.research_text import extract_text
        for index in range(22):
            self.source.write_text('needle' if index == 21 else f'unrelated {index}')
            meta = metadata()
            meta['source_ref'] += str(index)
            archive_file(self.store, self.source, meta, text_result=extract_text(self.source, media_type='text/plain'))
        code, found = self.cli('find', '--query', 'needle')
        self.assertEqual(code, 0)
        self.assertEqual(len(found['items']), 1)
        self.assertFalse(found['partial'])

    def test_uninitialized_is_distinct_from_no_matches(self):
        code, result = self.cli('find')
        self.assertEqual(code, 1)
        self.assertEqual(result['code'], 'uninitialized')
        self.put()
        self.assertEqual(self.cli('find', '--query', 'absent')[1]['items'], [])

    def test_discover_needs_no_store_and_does_not_import(self):
        code, result = self.cli('discover', '--root', self.root, '--symbol', 'ALPHA.US', configured=False)
        self.assertEqual(code, 0)
        self.assertEqual(len(result['items']), 1)
        self.assertFalse(self.store.exists())

    def test_cli_rejects_invalid_inputs_without_a_traceback(self):
        code, result = self.cli('put', '--file', self.source)
        self.assertEqual(code, 2)
        self.assertEqual(result['status'], 'error')
        self.meta.write_text('{broken')
        self.assertEqual(self.cli('put', '--file', self.source, '--metadata', self.meta)[0], 2)

    def test_transcript_import_preserves_result_and_never_claims_full_document(self):
        transcript = self.root / 'session.jsonl'
        entries = [
            {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'a', 'name': 'WebFetch'}]}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'a', 'content': '现金流原始返回', 'is_error': False}]}},
        ]
        transcript.write_text('\n'.join(map(json.dumps, entries)) + '\n')
        code, saved = self.cli('put', '--transcript', transcript, '--format', 'claude-jsonl', '--call-id', 'a', '--metadata', self.meta)
        self.assertEqual(code, 0, saved)
        self.assertEqual(saved['completeness'], 'unknown')
        self.assertEqual(saved['representation'], 'tool_response')
        self.assertEqual(Path(saved['raw_path']).read_text(), '现金流原始返回')


if __name__ == '__main__':
    unittest.main()
