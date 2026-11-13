"""Local source archive. Archival is evidence retention, never valuation approval."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from urllib.parse import parse_qsl, urlsplit
import uuid


class ResearchError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def resolve_store(value: str | None, environ: Mapping[str, str]) -> Path:
    selected = value or environ.get('LYNCH_RESEARCH_DIR')
    if not selected or not Path(selected).is_absolute():
        raise ResearchError('configuration', '资料库需要显式的绝对路径：--store 或 LYNCH_RESEARCH_DIR')
    return Path(selected).resolve()


def sensitive(value: str) -> bool:
    """Reject known credential shapes, without claiming general secret detection."""
    return bool(re.search(r'(?i)(authorization\s*[:=]|bearer\s+\S+|(?:api[_-]?key|access[_-]?token|password|cookie)\s*[:=]\s*\S+)', value))


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _metadata(value: dict) -> dict:
    required = ('title', 'provider', 'source_ref', 'kind', 'completeness', 'relevance')
    if not isinstance(value, dict) or any(not isinstance(value.get(k), str) or not value[k].strip() for k in required):
        raise ResearchError('metadata', '资料缺少标题、来源、类型、完整性或相关性说明')
    if value['kind'] not in ('filing', 'report', 'transcript', 'webpage', 'financial_data', 'search_result'):
        raise ResearchError('metadata', '不支持的原始资料类型')
    if value['completeness'] not in ('full', 'excerpt', 'search_snippet', 'unknown'):
        raise ResearchError('metadata', '资料完整性标记无效')
    lists = ('symbols', 'topics', 'report_periods', 'metric_names', 'assumption_refs')
    for name in lists:
        if name in value and (not isinstance(value[name], list) or any(not isinstance(v, str) or not v.strip() for v in value[name])):
            raise ResearchError('metadata', '资料关联和覆盖范围必须是非空字符串列表')
    if not value.get('symbols') and not value.get('topics'):
        raise ResearchError('metadata', '资料需要关联标的或研究主题')
    for name in ('published_at', 'fetched_at'):
        if value.get(name) is not None:
            try:
                datetime.fromisoformat(value[name])
            except (ValueError, TypeError):
                raise ResearchError('metadata', '资料日期需要 ISO 8601 格式') from None
    source = urlsplit(value['source_ref'])
    private_keys = {'token', 'key', 'apikey', 'api_key', 'access_token', 'signature', 'password', 'authorization'}
    if source.username or source.password or any(k.lower() in private_keys for k, _ in parse_qsl(source.query)) or sensitive(_json(value)):
        raise ResearchError('sensitive', '资料来源或描述含疑似身份信息，未归档')
    allowed = set(required) | set(lists) | {'media_type', 'published_at', 'fetched_at', 'frequency', 'currency',
                                        'framework_ref', 'conversation_ref', 'batch_ref', 'import_provenance', 'representation'}
    if set(value) - allowed:
        raise ResearchError('metadata', '资料描述包含未知字段')
    return json.loads(_json(value))


_SCHEMA = '''
CREATE TABLE IF NOT EXISTS objects (hash TEXT PRIMARY KEY, size INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS materials (
 id TEXT PRIMARY KEY, provider TEXT NOT NULL, source_ref TEXT NOT NULL,
 hash TEXT NOT NULL REFERENCES objects(hash), metadata TEXT NOT NULL,
 archived_at TEXT NOT NULL, text_hash TEXT, extraction TEXT NOT NULL,
 UNIQUE(provider, source_ref, hash));
CREATE TABLE IF NOT EXISTS acquisitions (
 material_id TEXT NOT NULL REFERENCES materials(id), fingerprint TEXT NOT NULL,
 metadata TEXT NOT NULL, PRIMARY KEY(material_id, fingerprint));
CREATE TABLE IF NOT EXISTS associations (
 material_id TEXT NOT NULL REFERENCES materials(id), kind TEXT NOT NULL, value TEXT NOT NULL,
 PRIMARY KEY(material_id, kind, value));
'''


def _connect(store: Path, *, write: bool = False) -> sqlite3.Connection:
    if not store.is_absolute():
        raise ResearchError('configuration', '资料库路径必须是绝对路径')
    db = store / 'catalog.sqlite3'
    if db.is_symlink():
        raise ResearchError('path', '资料目录文件不能是符号链接')
    if write:
        store.mkdir(parents=True, exist_ok=True)
    elif not db.is_file():
        raise ResearchError('uninitialized', '资料库尚未初始化，不能当作已检索的空库')
    connection = None
    try:
        connection = sqlite3.connect(str(db) if write else db.as_uri() + '?mode=ro', uri=not write, timeout=30)
        connection.row_factory = sqlite3.Row
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        if version not in ((0, 1) if write else (1,)):
            raise ResearchError('schema', '资料库版本不支持')
        connection.execute('PRAGMA foreign_keys=ON')
        if write:
            connection.executescript(_SCHEMA)
            connection.execute('PRAGMA user_version=1')
        return connection
    except (sqlite3.Error, ResearchError) as error:
        if connection is not None:
            connection.close()
        if isinstance(error, ResearchError):
            raise
        raise ResearchError('database', '资料目录无法读取或初始化') from None


def _object_path(store: Path, digest: str, name: str) -> Path:
    if not re.fullmatch('[0-9a-f]{64}', digest):
        raise ResearchError('integrity', '资料内容标识损坏')
    path = store / 'objects' / digest / name
    if any(p.is_symlink() for p in (store / 'objects', path.parent, path)):
        raise ResearchError('path', '资料对象路径不能是符号链接')
    return path


def _publish(store: Path, content: bytes, name: str) -> str:
    digest = hashlib.sha256(content).hexdigest()
    destination = _object_path(store, digest, name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.read_bytes() != content:
            raise ResearchError('integrity', '已存资料内容与摘要不符')
        return digest
    staging = store / 'staging'
    if staging.is_symlink():
        raise ResearchError('path', '资料暂存目录不能是符号链接')
    staging.mkdir(exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=staging, delete=False) as output:
            temporary = Path(output.name)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return digest


def archive_file(store: Path, source: Path, metadata: dict, *, text_result: dict | None = None) -> dict:
    meta = _metadata(metadata)
    connection = None
    try:
        content = source.read_bytes()
        if not content:
            raise ResearchError('empty', '没有可保存的原始资料')
        connection = _connect(store, write=True)
        digest = hashlib.sha256(content).hexdigest()
        reused_content = _object_path(store, digest, 'raw').exists()
        _publish(store, content, 'raw')
        extraction = dict(text_result or {'status': 'unavailable', 'reason': 'not_extracted', 'page_starts': []})
        text = extraction.pop('text', None)
        text_hash = _publish(store, text.encode(), 'text.txt') if extraction['status'] == 'ready' and text else None
        if not text_hash:
            extraction['status'] = 'unavailable'
        material_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        with connection:
            connection.execute('INSERT OR IGNORE INTO objects VALUES (?, ?)', (digest, len(content)))
            connection.execute('INSERT OR IGNORE INTO materials VALUES (?,?,?,?,?,?,?,?)',
                               (material_id, meta['provider'], meta['source_ref'], digest, _json(meta), now, text_hash, _json(extraction),))
            row = connection.execute('SELECT id FROM materials WHERE provider=? AND source_ref=? AND hash=?',
                                     (meta['provider'], meta['source_ref'], digest)).fetchone()
            material_id = row['id']
            if text_hash:
                connection.execute('UPDATE materials SET text_hash=?,extraction=? WHERE id=? AND text_hash IS NULL',
                                   (text_hash, _json(extraction), material_id))
            connection.execute('INSERT OR IGNORE INTO acquisitions VALUES (?,?,?)',
                               (material_id, hashlib.sha256(_json(meta).encode()).hexdigest(), _json(meta)))
            for kind in ('symbols', 'topics', 'report_periods', 'assumption_refs'):
                connection.executemany('INSERT OR IGNORE INTO associations VALUES (?,?,?)',
                                       [(material_id, kind, value) for value in meta.get(kind, [])])
        saved = get_material(store, material_id)
        saved['reused_content'] = reused_content
        return saved
    except (OSError, sqlite3.Error):
        raise ResearchError('io', '原始资料保存失败；未报告成功') from None
    finally:
        if connection is not None:
            connection.close()


def _public(store, row):
    result = json.loads(row['metadata'])
    result.update(material_id=row['id'], content_sha256=row['hash'], archived_at=row['archived_at'],
                  representation=result.get('representation', 'source_file'),
                  raw_path=str(_object_path(store, row['hash'], 'raw')),
                  text_path=str(_object_path(store, row['text_hash'], 'text.txt')) if row['text_hash'] else None)
    result.update({f'text_{k}' if k in ('status', 'reason') else k: v for k, v in json.loads(row['extraction']).items()})
    return result


def get_material(store: Path, material_id: str) -> dict:
    connection = _connect(store)
    try:
        row = connection.execute('SELECT * FROM materials WHERE id=?', (material_id,)).fetchone()
        if row is None:
            raise ResearchError('missing', '指定资料不存在')
        result = _public(store, row)
        for key, digest in (('raw_path', row['hash']), ('text_path', row['text_hash'])):
            if result[key] and hashlib.sha256(Path(result[key]).read_bytes()).hexdigest() != digest:
                raise ResearchError('integrity', '资料文件内容与登记摘要不符')
        result['acquisitions'] = [json.loads(r[0]) for r in connection.execute('SELECT metadata FROM acquisitions WHERE material_id=?', (material_id,))]
        return result
    except (OSError, sqlite3.Error):
        raise ResearchError('integrity', '资料文件缺失或目录无法读取') from None
    finally:
        connection.close()


def find_materials(store: Path, filters: dict, *, limit: int = 20, offset: int = 0) -> list[dict]:
    if not 1 <= limit <= 2001 or offset < 0:
        raise ResearchError('input', '资料分页范围无效')
    where, values = [], []
    for key, selected in filters.items():
        if not selected:
            continue
        selected = selected if isinstance(selected, list) else [selected]
        placeholders = ','.join('?' for _ in selected)
        if key in ('symbol', 'topic', 'period'):
            kind = {'symbol': 'symbols', 'topic': 'topics', 'period': 'report_periods'}[key]
            where.append(f'EXISTS (SELECT 1 FROM associations a WHERE a.material_id=m.id AND a.kind=? AND a.value IN ({placeholders}))')
            values.extend([kind, *selected])
        elif key in ('source_ref', 'provider'):
            where.append(f'm.{key} IN ({placeholders})')
            values.extend(selected)
        elif key == 'kind':
            where.append(f"json_extract(m.metadata, '$.kind') IN ({placeholders})")
            values.extend(selected)
        elif key in ('published_from', 'published_to'):
            where.append("json_extract(m.metadata, '$.published_at') " + ('>=' if key.endswith('from') else '<=') + ' ?')
            values.append(selected[0])
        else:
            raise ResearchError('input', '不支持的资料筛选条件')
    connection = _connect(store)
    try:
        query = 'SELECT m.* FROM materials m' + (' WHERE ' + ' AND '.join(where) if where else '')
        rows = connection.execute(query + ' ORDER BY m.archived_at,m.id LIMIT ? OFFSET ?', [*values, limit, offset]).fetchall()
        return [_public(store, row) for row in rows]
    except sqlite3.Error:
        raise ResearchError('database', '资料目录查询失败') from None
    finally:
        connection.close()
