from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from eval.lib.research_store import ResearchError, archive_file
from eval.lib.research_text import extract_text, read_excerpt, search_text
from eval.lib.test_research_store import metadata


class TextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2] / '_local/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def material(self, body):
        source = self.root / 'report.txt'
        source.write_text(body)
        return archive_file(self.root / 'store', source, metadata(), text_result=extract_text(source, media_type='text/plain'))

    def test_chinese_literal_search_returns_original_lines(self):
        material = self.material('财报\n经营现金流 123\n资本开支 45\n备注')
        hits = search_text(material, '资本开支')
        self.assertEqual(hits[0]['line'], 3)
        self.assertIn('经营现金流 123', hits[0]['text'])
        self.assertEqual(search_text(material, 'not present'), [])

    def test_search_metacharacters_are_literal(self):
        material = self.material('plain text\n.* is literal')
        self.assertEqual([h['line'] for h in search_text(material, '.*')], [2])
        with patch('eval.lib.research_text.subprocess.run', side_effect=FileNotFoundError):
            with self.assertRaises(ResearchError):
                search_text(material, 'plain')

    def test_read_table_across_pdf_page_boundary(self):
        material = self.material('表头\n现金 123\n设备 456\n附注')
        material['page_starts'] = [1, 3]
        self.assertIn('设备 456', read_excerpt(material, start_line=2, end_line=4)['text'])
        self.assertEqual(read_excerpt(material, page=2)['start_line'], 3)
        small = read_excerpt(material, max_chars=4)
        self.assertTrue(small['truncated'])
        self.assertIsNotNone(small['next_line'])

    def test_html_ignores_scripts_but_keeps_original_file(self):
        source = self.root / 'page.html'
        body = '<p>现金流</p><script>secret code</script><p>123</p>'
        source.write_text(body)
        text = extract_text(source, media_type='text/html')
        self.assertIn('现金流', text['text'])
        self.assertNotIn('secret code', text['text'])
        self.assertEqual(source.read_text(), body)

    def test_extraction_failures_are_not_empty_searches(self):
        source = self.root / 'scan.pdf'
        source.write_bytes(b'not a PDF')
        with patch('eval.lib.research_text.subprocess.run', side_effect=FileNotFoundError):
            self.assertEqual(extract_text(source, media_type='application/pdf')['reason'], 'pdftotext_missing')
        self.assertEqual(extract_text(source, media_type='application/pdf')['status'], 'unavailable')
        material = archive_file(self.root / 'store', source, metadata())
        with self.assertRaises(ResearchError):
            search_text(material, 'cash')
        with self.assertRaises(ResearchError):
            read_excerpt(material)

    @unittest.skipUnless(shutil.which('pdftotext'), 'pdftotext unavailable')
    def test_actual_pdf_extraction_preserves_page_mapping(self):
        objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
                   b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
                   b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
        stream = b'BT /F1 12 Tf 10 200 Td (Cash flow 123) Tj ET'
        objects.append(b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream')
        data, offsets = b'%PDF-1.4\n', [0]
        for index, obj in enumerate(objects, 1):
            offsets.append(len(data))
            data += str(index).encode() + b' 0 obj\n' + obj + b'\nendobj\n'
        xref = len(data)
        data += b'xref\n0 6\n0000000000 65535 f \n'
        data += b''.join(f'{offset:010} 00000 n \n'.encode() for offset in offsets[1:])
        data += f'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()
        source = self.root / 'report.pdf'
        source.write_bytes(data)
        extracted = extract_text(source, media_type='application/pdf')
        self.assertEqual(extracted['status'], 'ready')
        self.assertIn('Cash flow 123', extracted['text'])
        self.assertEqual(extracted['page_starts'], [1])


if __name__ == '__main__':
    unittest.main()
