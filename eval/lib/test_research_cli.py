import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

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

    def test_metadata_requires_a_file_path_instead_of_inline_json(self):
        for value in (json.dumps(metadata()), '{broken', '[1, 2]'):
            with self.subTest(value=value):
                code, result = self.cli('put', '--file', self.source, '--metadata', value)
                self.assertEqual(code, 2, result)
                self.assertEqual(result['code'], 'input')
                self.assertIn('--metadata', result['message'])
                self.assertIn('JSON 文件路径', result['message'])
                self.assertNotIn(value, result['message'])
        self.assertFalse(self.store.exists())

    def test_missing_metadata_file_is_an_input_error(self):
        code, result = self.cli('put', '--file', self.source, '--metadata', self.root / 'missing.json')
        self.assertEqual(code, 2, result)
        self.assertEqual(result['code'], 'input')
        self.assertIn('描述文件不存在', result['message'])
        self.assertIn('--metadata', result['message'])
        self.assertFalse(self.store.exists())

    def test_metadata_file_name_can_start_with_a_json_delimiter(self):
        self.meta = self.root / '[description].json'
        self.meta.write_text(json.dumps(metadata()))
        self.put()

    def test_unreadable_metadata_is_identified_before_accessing_the_store(self):
        code, result = self.cli('put', '--file', self.source, '--metadata', self.root)
        self.assertEqual(code, 1, result)
        self.assertEqual(result['code'], 'io')
        self.assertIn('描述文件无法读取', result['message'])
        self.assertFalse(self.store.exists())

    def test_json_like_metadata_path_preserves_permission_and_resource_errors(self):
        from eval.lib.research_cli import _read_metadata
        from eval.lib.research_store import ResearchError
        for name in ('[description].json', '{description}.json'):
            for number in (errno.EACCES, errno.EMFILE, errno.EISDIR):
                with self.subTest(name=name, number=number):
                    with patch.object(Path, 'read_text', side_effect=OSError(number, 'unavailable')):
                        with self.assertRaises(ResearchError) as raised:
                            _read_metadata(Path(name))
                    self.assertEqual(raised.exception.code, 'io')
                    self.assertIn('描述文件无法读取', str(raised.exception))

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
