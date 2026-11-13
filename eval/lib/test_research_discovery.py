from pathlib import Path
import tempfile
import unittest

from eval.lib.research_discovery import discover_local


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2] / '_local/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_find_symbol_directory_or_filename_without_reading_or_importing(self):
        raw = self.root / 'ALPHA.US/raw'
        raw.mkdir(parents=True)
        (raw / 'lb_CF.json').write_text('{"cash":123}')
        (self.root / 'alpha_cf.json').write_text('{}')
        (self.root / 'beta_cf.json').write_text('{}')
        found = discover_local([self.root], symbol='ALPHA.US')
        self.assertEqual(len(found['items']), 2)
        self.assertFalse(found['partial'])
        self.assertNotIn('fetched_at', found['items'][0])

    def test_budget_and_symlink_are_not_silently_reported_as_complete(self):
        for index in range(5):
            (self.root / f'alpha-{index}.txt').write_text('value')
        (self.root / 'linked').symlink_to(self.root, target_is_directory=True)
        found = discover_local([self.root], symbol='ALPHA.US', max_files=2)
        self.assertTrue(found['partial'])
        self.assertLessEqual(found['scanned'], 2)
        self.assertEqual(len(discover_local([self.root], name='absent')['items']), 0)

    def test_explicit_company_root_is_part_of_the_match(self):
        root = self.root / 'ALPHA.US'
        (root / 'raw').mkdir(parents=True)
        (root / 'raw/lb_CF.json').write_text('{}')
        self.assertEqual(len(discover_local([root], symbol='ALPHA.US')['items']), 1)


if __name__ == '__main__':
    unittest.main()
