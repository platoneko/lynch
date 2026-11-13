"""Explicit source archive commands; never fetch data or execute research code."""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
from pathlib import Path
import sys
import tempfile

from eval.lib.research_discovery import discover_local
from eval.lib.research_store import ResearchError, archive_file, find_materials, get_material, resolve_store
from eval.lib.research_text import extract_text, read_excerpt, search_text


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ResearchError('input', '命令参数无效，使用 --help 查看格式')


def _parser():
    parser = _Parser(description=__doc__)
    parser.add_argument('--store')
    commands = parser.add_subparsers(dest='action', required=True)
    put = commands.add_parser('put', help='登记已取得的原始资料')
    inputs = put.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--file', type=Path)
    inputs.add_argument('--transcript', type=Path)
    put.add_argument('--format', choices=('claude-jsonl', 'codex-jsonl'))
    put.add_argument('--call-id')
    put.add_argument('--thread-id')
    put.add_argument('--content-index', type=int)
    put.add_argument('--metadata', required=True, type=Path)
    find = commands.add_parser('find', help='按来源、覆盖范围和关键词查找')
    for flag in ('source-ref', 'symbol', 'topic', 'period', 'kind'):
        find.add_argument('--' + flag, action='append')
    for flag in ('published-from', 'published-to', 'query'):
        find.add_argument('--' + flag)
    find.add_argument('--limit', type=int, default=20)
    find.add_argument('--offset', type=int, default=0)
    read = commands.add_parser('read', help='有界读取指定资料原文')
    read.add_argument('material_id')
    read.add_argument('--from-line', type=int, default=1)
    read.add_argument('--to-line', type=int)
    read.add_argument('--page', type=int)
    read.add_argument('--max-chars', type=int, default=12000)
    read.add_argument('--raw-path', action='store_true')
    discover = commands.add_parser('discover', help='在指定本地目录发现未登记文件，不归档')
    discover.add_argument('--root', type=Path, action='append', required=True)
    discover.add_argument('--symbol')
    discover.add_argument('--name')
    discover.add_argument('--max-files', type=int, default=2000)
    discover.add_argument('--max-depth', type=int, default=6)
    discover.add_argument('--limit', type=int, default=20)
    for command in (put, find, read, discover):
        command.add_argument('--store', default=argparse.SUPPRESS)
    return parser


def _find(store, args):
    if not 1 <= args.limit <= 100 or args.offset < 0:
        raise ResearchError('input', '查询数量需要在 1 至 100 之间，分页位置不能为负')
    filters = {key: getattr(args, key) for key in ('source_ref', 'symbol', 'topic', 'period', 'kind', 'published_from', 'published_to')}
    items, scanned, unavailable = [], 0, 0
    offset, has_more = args.offset, False
    while scanned < 2000 and len(items) < args.limit:
        candidates = find_materials(store, filters, limit=min(100, 2000 - scanned), offset=offset)
        if not candidates:
            break
        for material in candidates:
            offset += 1
            scanned += 1
            material = get_material(store, material['material_id'])
            if args.query:
                if material['text_status'] != 'ready':
                    unavailable += 1
                    continue
                hits = search_text(material, args.query)
                if not hits:
                    continue
                material['hits'] = hits
            material.pop('acquisitions', None)
            items.append(material)
            if len(items) >= args.limit:
                break
        if len(candidates) < 100 and offset >= args.offset + scanned and len(items) < args.limit:
            break
    has_more = bool(find_materials(store, filters, limit=1, offset=offset))
    return {'items': items, 'scanned': scanned, 'text_unavailable': unavailable, 'has_more': has_more,
            'partial': has_more, 'next_offset': offset if has_more else None}


def _run(args):
    if args.action == 'discover':
        return discover_local(args.root, symbol=args.symbol, name=args.name,
                              max_files=args.max_files, max_depth=args.max_depth, limit=args.limit)
    store = resolve_store(args.store, os.environ)
    if args.action == 'put':
        try:
            metadata = json.loads(args.metadata.read_text(encoding='utf-8'))
        except (ValueError, UnicodeError):
            raise ResearchError('input', '资料描述需要有效的 UTF-8 JSON 对象') from None
        if not isinstance(metadata, dict):
            raise ResearchError('input', '资料描述需要 JSON 对象')
        if args.transcript:
            from eval.lib.research_import import extract_tool_result
            if not args.format or not args.call_id:
                raise ResearchError('input', '会话导入需要 --format 和 --call-id')
            response = extract_tool_result(args.transcript, format=args.format, call_id=args.call_id,
                                           thread_id=args.thread_id, content_index=args.content_index)
            metadata.update(representation='tool_response', import_provenance=response['provenance'],
                            completeness=response['completeness'], media_type=response['media_type'])
            # Scratch stays inside the explicitly configured store, never /tmp.
            staging_root = store / 'staging'
            if staging_root.is_symlink():
                raise ResearchError('path', '资料暂存目录不能是符号链接')
            staging_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='import-', dir=staging_root) as staging:
                source = Path(staging) / 'response.txt'
                source.write_bytes(response['bytes'])
                return archive_file(store, source, metadata, text_result=extract_text(source, media_type=response['media_type']))
        if any(value is not None for value in (args.format, args.call_id, args.thread_id, args.content_index)):
            raise ResearchError('input', '工具调用参数只能与 --transcript 使用')
        metadata['representation'] = 'source_file'
        media_type = metadata.get('media_type') if isinstance(metadata, dict) else None
        media_type = media_type or mimetypes.guess_type(args.file.name)[0] or 'application/octet-stream'
        extracted = extract_text(args.file, media_type=media_type)
        return archive_file(store, args.file, metadata, text_result=extracted)
    if args.action == 'find':
        return _find(store, args)
    material = get_material(store, args.material_id)
    if args.raw_path:
        return {'material_id': material['material_id'], 'source_ref': material['source_ref'], 'raw_path': material['raw_path'],
                'content_sha256': material['content_sha256'], 'completeness': material['completeness']}
    return read_excerpt(material, start_line=args.from_line, end_line=args.to_line,
                        page=args.page, max_chars=args.max_chars)


def main(argv: list[str] | None = None) -> int:
    try:
        result = _run(_parser().parse_args(argv))
        print(json.dumps({'status': 'ok', **result}, ensure_ascii=False, allow_nan=False))
        return 0
    except ResearchError as error:
        print(json.dumps({'status': 'error', 'code': error.code, 'message': str(error)}, ensure_ascii=False))
        return 2 if error.code in ('input', 'metadata', 'configuration', 'sensitive') else 1
    except OSError:
        print(json.dumps({'status': 'error', 'code': 'io', 'message': '资料文件或目录无法访问'}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    sys.exit(main())
