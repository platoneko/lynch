import concurrent.futures
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from eval.lib.research_store import ResearchError, archive_file, find_materials, get_material, resolve_store


def metadata(**changes):
    return dict(title='年度现金流', provider='sample', source_ref='https://example.org/report',
                kind='financial_data', completeness='full', relevance='核对资本开支',
                symbols=['ALPHA.US'], report_periods=['FY2025'], **changes)


def put_worker(args):
    return archive_file(*args)['material_id']


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2] / '_local/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = self.root / 'store'
        self.source = self.root / 'cash.json'
        self.source.write_text('{"现金流":"123.00"}')

    def test_archive_survives_removed_input(self):
        result = archive_file(self.store, self.source, metadata())
        self.source.unlink()
        saved = get_material(self.store, result['material_id'])
        self.assertEqual(Path(saved['raw_path']).read_text(), '{"现金流":"123.00"}')
        self.assertIsNone(saved.get('fetched_at'))
        self.assertTrue(saved['archived_at'])

    def test_same_bytes_preserve_two_sources_and_associations(self):
        first = archive_file(self.store, self.source, metadata())
        other = metadata()
        other.update(source_ref='https://example.org/other', symbols=['BETA.US'])
        second = archive_file(self.store, self.source, other)
        self.assertNotEqual(first['material_id'], second['material_id'])
        self.assertEqual(first['content_sha256'], second['content_sha256'])
        self.assertEqual(len(list((self.store / 'objects').glob('*/raw'))), 1)
        repeated = metadata()
        repeated['symbols'] = ['GAMMA.US']
        self.assertEqual(archive_file(self.store, self.source, repeated)['material_id'], first['material_id'])
        self.assertEqual(find_materials(self.store, {'symbol': ['GAMMA.US']})[0]['material_id'], first['material_id'])

    def test_changed_source_keeps_both_versions(self):
        old = archive_file(self.store, self.source, metadata())
        self.source.write_text('updated')
        new = archive_file(self.store, self.source, metadata())
        self.assertNotEqual(new['material_id'], old['material_id'])
        self.assertIn('123.00', Path(get_material(self.store, old['material_id'])['raw_path']).read_text())
        self.assertEqual(len(find_materials(self.store, {'source_ref': ['https://example.org/report']})), 2)

    def test_concurrent_processes_reuse_the_same_material(self):
        with concurrent.futures.ProcessPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(put_worker, [(self.store, self.source, metadata())] * 2))
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(len(find_materials(self.store, {})), 1)

    def test_failed_publish_does_not_create_successful_record(self):
        with patch('eval.lib.research_store.os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(ResearchError):
                archive_file(self.store, self.source, metadata())
        if (self.store / 'catalog.sqlite3').exists():
            self.assertEqual(find_materials(self.store, {}), [])

    def test_read_is_readonly_and_config_never_guesses(self):
        with self.assertRaises(ResearchError):
            find_materials(self.store, {})
        self.assertFalse(self.store.exists())
        with self.assertRaises(ResearchError):
            resolve_store(None, {})
        with self.assertRaises(ResearchError):
            resolve_store('relative', {})
        self.assertEqual(resolve_store(str(self.store), {'LYNCH_RESEARCH_DIR': '/other'}), self.store)

    def test_missing_object_and_future_schema_are_errors(self):
        saved = archive_file(self.store, self.source, metadata())
        Path(saved['raw_path']).unlink()
        with self.assertRaises(ResearchError):
            get_material(self.store, saved['material_id'])
        import sqlite3
        with sqlite3.connect(self.store / 'catalog.sqlite3') as connection:
            connection.execute('PRAGMA user_version=99')
        with self.assertRaises(ResearchError):
            find_materials(self.store, {})

    def test_sensitive_source_and_invalid_metadata_rejected(self):
        for change in ({'source_ref': 'https://example.org/a?token=private'}, {'symbols': []},
                       {'fetched_at': 'yesterday'}, {'kind': 'analysis'}):
            value = metadata()
            value.update(change)
            with self.subTest(change=change), self.assertRaises(ResearchError):
                archive_file(self.store, self.source, value)

    def test_symlinked_object_is_not_followed(self):
        saved = archive_file(self.store, self.source, metadata())
        target = Path(saved['raw_path'])
        target.unlink()
        target.symlink_to(self.source)
        with self.assertRaises(ResearchError):
            get_material(self.store, saved['material_id'])

    def test_reimport_can_add_text_without_changing_original_version(self):
        first = archive_file(self.store, self.source, metadata())
        self.assertEqual(first['text_status'], 'unavailable')
        from eval.lib.research_text import extract_text
        second = archive_file(self.store, self.source, metadata(),
                              text_result=extract_text(self.source, media_type='application/json'))
        self.assertEqual(first['material_id'], second['material_id'])
        self.assertEqual(second['text_status'], 'ready')

    def test_changed_input_cannot_bind_old_text_to_new_raw(self):
        from eval.lib.research_text import extract_text
        extracted = extract_text(self.source, media_type='application/json')
        self.source.write_text('new cash flow')
        with self.assertRaises(ResearchError):
            archive_file(self.store, self.source, metadata(), text_result=extracted)

    def test_reimport_exposes_all_associations_and_fills_missing_source_date(self):
        archive_file(self.store, self.source, metadata())
        revised = metadata()
        revised.update(symbols=['BETA.US'], report_periods=['FY2024'], published_at='2026-11-13T08:00:00Z')
        archive_file(self.store, self.source, revised)
        found = find_materials(self.store, {'symbol': ['BETA.US'], 'period': ['FY2024']})[0]
        self.assertIn('BETA.US', found['symbols'])
        self.assertIn('FY2024', found['report_periods'])
        self.assertEqual(found['published_at'], '2026-11-13T08:00:00Z')

    def test_publication_day_is_inclusive_and_timestamp_offsets_compare_by_instant(self):
        value = metadata()
        value['published_at'] = '2026-11-13T10:00:00+08:00'
        archive_file(self.store, self.source, value)
        self.assertEqual(len(find_materials(self.store, {'published_to': '2026-11-13'})), 1)
        self.assertEqual(len(find_materials(self.store, {'published_from': '2026-11-13T01:00:00Z'})), 1)
        self.assertEqual(find_materials(self.store, {'published_to': '2026-11-13T01:00:00Z'}), [])


if __name__ == '__main__':
    unittest.main()
