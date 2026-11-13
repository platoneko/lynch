import json
from pathlib import Path
import tempfile
import unittest

from eval.lib.research_import import extract_tool_result
from eval.lib.research_store import ResearchError


class ImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2] / '_local/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'session.jsonl'

    def write(self, entries):
        self.path.write_text('\n'.join(json.dumps(e, ensure_ascii=False) for e in entries) + '\n')

    def call(self, ident='call-1', **extra):
        return {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': ident, 'name': 'mcp__source__report', 'input': {}}]}, **extra}

    def result(self, text='现金流 123', ident='call-1', **extra):
        return {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': ident, 'content': text, 'is_error': False}]}, **extra}

    def test_import_exact_call_excludes_other_messages(self):
        self.write([{'type': 'user', 'message': {'content': 'private user words'}}, self.call(), self.result(), self.call('other'), self.result('other body', 'other')])
        result = extract_tool_result(self.path, format='claude-jsonl', call_id='call-1')
        self.assertEqual(result['bytes'], '现金流 123'.encode())
        self.assertEqual(result['representation'], 'tool_response')
        self.assertEqual(result['completeness'], 'unknown')
        self.assertNotIn('private', str(result))

    def test_child_call_does_not_match_parent(self):
        self.write([self.call(), self.result('parent'), self.call(parent_tool_use_id='child'), self.result('child body', parent_tool_use_id='child')])
        with self.assertRaises(ResearchError):
            extract_tool_result(self.path, format='claude-jsonl', call_id='call-1')
        self.assertEqual(extract_tool_result(self.path, format='claude-jsonl', call_id='call-1', thread_id='child')['bytes'], b'child body')

    def test_updated_inputs_do_not_duplicate_result(self):
        self.write([self.call(), self.call(), self.result()])
        self.assertEqual(extract_tool_result(self.path, format='claude-jsonl', call_id='call-1')['bytes'], '现金流 123'.encode())

    def test_codex_multiblock_requires_explicit_selection(self):
        self.write([{'type': 'response_item', 'payload': {'type': 'custom_tool_call', 'call_id': 'a', 'name': 'exec'}},
                    {'type': 'response_item', 'payload': {'type': 'custom_tool_call_output', 'call_id': 'a', 'output': [{'type': 'input_text', 'text': 'first'}, {'type': 'input_text', 'text': 'second'}]}}])
        with self.assertRaises(ResearchError):
            extract_tool_result(self.path, format='codex-jsonl', call_id='a')
        selected = extract_tool_result(self.path, format='codex-jsonl', call_id='a', content_index=1)
        self.assertEqual(selected['bytes'], b'second')

    def test_failure_missing_ambiguous_and_sensitive_results_rejected(self):
        variants = [[self.call()], [self.call(), self.result(), self.result('different')],
                    [self.call(), self.result('Authorization: Bearer private')],
                    [self.call(), self.result('{"api_key":"synthetic-test-value"}')],
                    [self.call(), self.result('{"access_token":"synthetic-test-value"}')],
                    [self.call(), self.result('{"cookie":"synthetic-test-value"}')]]
        failed = self.result()
        failed['message']['content'][0]['is_error'] = True
        variants.append([self.call(), failed])
        for entries in variants:
            self.write(entries)
            with self.subTest(entries=len(entries)), self.assertRaises(ResearchError):
                extract_tool_result(self.path, format='claude-jsonl', call_id='call-1')

    def test_truncation_markers_are_excerpts_and_empty_results_fail(self):
        self.write([self.call(), self.result('original token count: 999\n...output truncated...')])
        self.assertEqual(extract_tool_result(self.path, format='claude-jsonl', call_id='call-1')['completeness'], 'excerpt')
        self.write([self.call(), self.result('')])
        with self.assertRaises(ResearchError):
            extract_tool_result(self.path, format='claude-jsonl', call_id='call-1')

    def test_native_failed_and_running_envelopes_are_not_successful_sources(self):
        for output in ('Process exited with code 1\nfailed body', 'Script running with cell ID 42',
                       '{"exit_code":1,"output":"failed body"}', '{"session_id":42,"exit_code":null,"output":"running"}'):
            self.write([{'type': 'response_item', 'payload': {'type': 'function_call', 'call_id': 'a', 'name': 'exec_command'}},
                        {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'a', 'output': output}}])
            with self.subTest(output=output), self.assertRaises(ResearchError):
                extract_tool_result(self.path, format='codex-jsonl', call_id='a')


if __name__ == '__main__':
    unittest.main()
