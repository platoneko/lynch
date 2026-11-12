"""Append-only historical axis annotations, bound to accepted result versions."""
import base64
import hashlib
import json
from pathlib import PurePosixPath
import sqlite3


def _encode(payload, ticker=None):
    if (not isinstance(payload, dict) or type(payload.get('complete')) is not bool
            or payload.get('relationship_to_result') != 'unverified'
            or not isinstance(payload.get('axis_metadata'), dict)
            or not isinstance(payload.get('source_versions'), dict)
            or not isinstance(payload.get('sources'), list) or not payload['sources']):
        raise ValueError('historical axis evidence is incomplete')
    for source in payload['sources']:
        if not isinstance(source, dict) or not isinstance(source.get('reference'), str) or not source['reference'].strip():
            raise ValueError('historical source reference missing')
        try:
            raw = base64.b64decode(source['content_base64'], validate=True)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError('historical source invalid') from exc
        if not raw or hashlib.sha256(raw).hexdigest() != source.get('sha256'):
            raise ValueError('historical source digest differs')
    available = {source['sha256'] for source in payload['sources']}
    declared = payload['source_versions']
    if (set(declared) != {'axis:' + alias for alias in payload['axis_metadata']}
            or any(digest not in available for digest in declared.values())):
        raise ValueError('axis source is not among historical documents')
    for alias, axis in payload['axis_metadata'].items():
        if not isinstance(axis, dict) or not isinstance(axis.get('axis_id'), str):
            raise ValueError('historical axis identity missing')
        name = axis['axis_id'] + '.md'
        digest = declared['axis:' + alias]
        if not any(source['sha256'] == digest and PurePosixPath(source['reference']).name == name
                   for source in payload['sources']):
            raise ValueError('historical axis source reference differs')
    if payload.get('own_consensus') is not None:
        own_source = payload.get('own_consensus_source')
        if not isinstance(own_source, dict):
            raise ValueError('standalone consensus source missing')
        reference, digest = own_source.get('reference'), own_source.get('sha256')
        if (not isinstance(reference, str) or not isinstance(digest, str)
                or (ticker is not None and PurePosixPath(reference).name != ticker + '.md')
                or not any(s['reference'] == reference and s['sha256'] == digest for s in payload['sources'])):
            raise ValueError('standalone consensus source differs')
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
    if len(encoded) > 4 * 1024 * 1024:
        raise ValueError('historical axis evidence too large')
    return encoded


def accept_history(connection, ticker, version, payload, *, expected_digest=None):
    document = _encode(payload, ticker)
    digest = hashlib.sha256(document).hexdigest()
    try:
        connection.execute('BEGIN IMMEDIATE')
        current = connection.execute('SELECT version, review_id FROM results WHERE ticker=?', (ticker,)).fetchone()
        if current is None or current[0] != version or not current[1].startswith('legacy:'):
            raise ValueError('historical result version changed or is not historical')
        batch = connection.execute('SELECT b.manifest,b.digest FROM legacy_items i JOIN legacy_batches b '
                                   'ON b.id=i.batch_id WHERE i.ticker=? AND i.version=? AND ?=(\'legacy:\' || i.batch_id)',
                                   (ticker, version, current[1])).fetchone()
        if batch is None:
            raise ValueError('historical result evidence missing')
        manifest = bytes(batch[0])
        if (hashlib.sha256(manifest).hexdigest() != batch[1]
                or json.loads(manifest).get('purpose', 'historical_migration') != 'historical_migration'):
            raise ValueError('result is not an intact historical migration')
        connection.execute('CREATE TABLE IF NOT EXISTS historical_axes '
                           '(ticker TEXT NOT NULL, version TEXT NOT NULL, revision INTEGER NOT NULL, document BLOB NOT NULL, '
                           'digest TEXT NOT NULL, PRIMARY KEY (ticker, version, revision))')
        connection.execute("CREATE TRIGGER IF NOT EXISTS immutable_historical_axes_update BEFORE UPDATE ON historical_axes "
                           "BEGIN SELECT RAISE(ABORT, 'immutable historical axes'); END")
        connection.execute("CREATE TRIGGER IF NOT EXISTS immutable_historical_axes_delete BEFORE DELETE ON historical_axes "
                           "BEGIN SELECT RAISE(ABORT, 'immutable historical axes'); END")
        existing = connection.execute('SELECT revision, document, digest FROM historical_axes WHERE ticker=? AND version=? '
                                      'ORDER BY revision DESC LIMIT 1',
                                      (ticker, version)).fetchone()
        if existing is None:
            if expected_digest is not None:
                raise ValueError('no previous history to correct')
            connection.execute('INSERT INTO historical_axes VALUES (?,?,?,?,?)', (ticker, version, 1, document, digest))
        elif bytes(existing[1]) == document and existing[2] == digest:
            pass  # Repeat the latest import, even if the caller retried after success.
        elif expected_digest != existing[2]:
            raise ValueError('historical axis version changed; correction requires its current digest')
        else:
            connection.execute('INSERT INTO historical_axes VALUES (?,?,?,?,?)',
                               (ticker, version, existing[0] + 1, document, digest))
        connection.commit()
        return digest
    except Exception:
        connection.rollback()
        raise


def read_history(connection, ticker, version):
    try:
        row = connection.execute('SELECT a.document,a.digest FROM historical_axes a JOIN results r '
                                 'ON r.ticker=a.ticker AND r.version=a.version '
                                 "WHERE a.ticker=? AND a.version=? AND r.review_id LIKE 'legacy:%' "
                                 'ORDER BY a.revision DESC LIMIT 1',
                                 (ticker, version)).fetchone()
    except sqlite3.OperationalError as exc:
        if 'no such table: historical_axes' in str(exc):
            return None
        raise
    if row is None:
        return None
    document = bytes(row[0])
    if hashlib.sha256(document).hexdigest() != row[1]:
        raise ValueError('historical axis supplement is corrupt')
    payload = json.loads(document)
    if _encode(payload, ticker) != document:
        raise ValueError('historical axis supplement is not canonical')
    return payload


def history_versions(connection, ticker, version):
    rows = connection.execute('SELECT revision,document,digest FROM historical_axes '
                              'WHERE ticker=? AND version=? ORDER BY revision', (ticker, version)).fetchall()
    versions = []
    for revision, raw, digest in rows:
        document = bytes(raw)
        payload = json.loads(document)
        if hashlib.sha256(document).hexdigest() != digest or _encode(payload, ticker) != document:
            raise ValueError('historical axis revision is corrupt')
        versions.append({'revision': revision, 'digest': digest, 'payload': payload})
    return versions
