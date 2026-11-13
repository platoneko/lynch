"""Derived searchable text, with bounded excerpts and original source identity."""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import subprocess

from eval.lib.research_store import ResearchError


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        elif tag in ('p', 'div', 'br', 'tr', 'h1', 'h2', 'h3', 'li') and not self.hidden:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)
        elif tag in ('td', 'th') and not self.hidden:
            self.parts.append('\t')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def extract_text(source: Path, *, media_type: str) -> dict:
    unavailable = {'status': 'unavailable', 'text': None, 'page_starts': []}
    try:
        if media_type == 'application/pdf':
            try:
                proc = subprocess.run(['pdftotext', '-layout', str(source), '-'], capture_output=True, timeout=60)
            except FileNotFoundError:
                return {**unavailable, 'reason': 'pdftotext_missing'}
            if proc.returncode:
                return {**unavailable, 'reason': 'pdf_extraction_failed'}
            text = proc.stdout.decode('utf-8')
        elif media_type.startswith('text/') or media_type in ('application/json', 'application/xml'):
            text = source.read_bytes().decode('utf-8')
        else:
            return {**unavailable, 'reason': 'unsupported_media_type'}
        if media_type == 'text/html':
            parser = _HTMLText()
            parser.feed(text)
            text = ''.join(parser.parts)
        if not text.strip():
            return {**unavailable, 'reason': 'no_text'}
        pages = text.split('\f') if media_type == 'application/pdf' else [text]
        if len(pages) > 1 and not pages[-1].strip():
            pages.pop()
        starts, lines = [], []
        for page in pages:
            starts.append(len(lines) + 1)
            lines.extend(page.split('\n'))
        return {'status': 'ready', 'reason': None, 'text': '\n'.join(lines),
                'page_starts': starts if media_type == 'application/pdf' else []}
    except (UnicodeError, OSError, subprocess.TimeoutExpired):
        return {**unavailable, 'reason': 'text_extraction_failed'}


def _lines(material):
    if material.get('text_status') != 'ready' or not material.get('text_path'):
        raise ResearchError('text_unavailable', '原文已保存，但目前不能全文检索或读取文本')
    try:
        return Path(material['text_path']).read_text(encoding='utf-8').split('\n')
    except (OSError, UnicodeError):
        raise ResearchError('text_unavailable', '已登记的可搜索文本无法读取') from None


def read_excerpt(material: dict, *, start_line: int = 1, end_line: int | None = None,
                 page: int | None = None, max_chars: int = 12000) -> dict:
    lines = _lines(material)
    if max_chars < 1 or max_chars > 100000 or start_line < 1:
        raise ResearchError('input', '读取范围或字符数无效')
    starts = material.get('page_starts', [])
    if page is not None:
        if start_line != 1 or end_line is not None or not 1 <= page <= len(starts):
            raise ResearchError('input', 'PDF 页范围无效，不能与行范围混用')
        start_line = starts[page - 1]
        end_line = starts[page] - 1 if page < len(starts) else len(lines)
    end_line = len(lines) if end_line is None else end_line
    if end_line < start_line or start_line > len(lines):
        raise ResearchError('input', '原文行范围无效')
    end_line = min(end_line, len(lines))
    text = '\n'.join(lines[start_line - 1:end_line])
    truncated = len(text) > max_chars
    selected = text[:max_chars]
    actual_end = start_line + selected.count('\n')
    return {'material_id': material['material_id'], 'source_ref': material['source_ref'],
            'title': material['title'], 'published_at': material.get('published_at'),
            'content_sha256': material['content_sha256'], 'completeness': material['completeness'],
            'text': selected, 'start_line': start_line, 'end_line': actual_end,
            'page': page, 'truncated': truncated, 'next_line': actual_end if truncated else None,
            'continuation_note': '扩大字符范围可读取被截取的长行' if truncated else None}


def search_text(material: dict, query: str, *, context_lines: int = 3, limit: int = 10) -> list[dict]:
    _lines(material)
    if not query or '\n' in query or '\r' in query or limit < 1 or context_lines < 0:
        raise ResearchError('input', '关键词需要单行非空文本和有效的结果范围')
    try:
        proc = subprocess.run(['rg', '--fixed-strings', '--line-number', '--no-filename',
                               '--max-count', str(limit), '--', query, material['text_path']],
                              capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise ResearchError('search_unavailable', '原文搜索工具不可用') from None
    if proc.returncode == 1:
        return []
    if proc.returncode != 0:
        raise ResearchError('search_failed', '原文搜索未完成')
    hits = []
    for line in proc.stdout.decode('utf-8').splitlines():
        number = int(line.split(':', 1)[0])
        excerpt = read_excerpt(material, start_line=max(1, number - context_lines),
                               end_line=number + context_lines, max_chars=3000)
        excerpt['line'] = number
        pages = material.get('page_starts', [])
        excerpt['page'] = sum(start <= number for start in pages) if pages else None
        hits.append(excerpt)
    return hits
