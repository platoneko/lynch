"""Local research-agent attestation after user authorization in conversation.

There is no HTTP endpoint. A nonempty message reference is an operator's claim,
not proof that the cited words express approval or that execution was isolated.
"""
import base64
import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import getpass
import hashlib
import os
from pathlib import Path
import secrets
import sys
import sqlite3
from types import SimpleNamespace

from eval.lib.valuation_drafts import Draft, MAX_RECORD_BYTES, RevisionConflict, _json_bytes, _load_json, _validate_draft
from eval.lib.runner_draft import draft_from_result
from eval.lib.valuation_runner import _validate_response, check_source_identity
from lynch_reader.archive import ARCHIVE_FILE, FrameworkArchive
from lynch_reader.legacy_batches import _capture, _hash, _source_observation
from lynch_reader.valuation_reviews import Captured, _check_units


@dataclass(frozen=True)
class ConversationAuthorization:
    """The agent cites the actual message; this object cannot verify its meaning."""
    session_reference: str
    user_authorization: str

    def __post_init__(self):
        if any(not isinstance(s, str) or not s.strip() for s in (self.session_reference, self.user_authorization)):
            raise ValueError('conversation and explicit authorization message are required')


def _validated(draft):
    if not isinstance(draft, Draft):
        raise ValueError('a frozen draft is required')
    document = _load_json(draft._document)
    _validate_draft(document)
    output = document['output']
    ticker = output['ticker']
    _validate_response(output, ticker)
    _check_units(output)
    if document['entry'] != f'content/eval/{ticker}.py':
        raise ValueError('draft source identity mismatch')
    for key, record in output['source_paths'].items():
        root = 'content' if key == 'stock' or key.startswith('axis:') else 'code'
        selected = Path(record['selected'])
        expected_root = Path(output['root_identity'][f'{root}_resolved'])
        name = root + '/' + selected.relative_to(expected_root).as_posix()
        captured = base64.b64decode(document['inputs'][name], validate=True)
        if hashlib.sha256(captured).hexdigest() != output['source_versions'][key]:
            raise ValueError('frozen source differs from calculation output')
    external_name = f'external/{ticker}.json'
    external = (_load_json(base64.b64decode(document['inputs'][external_name], validate=True))
                if external_name in document['inputs'] else None)
    check_source_identity(output, external_inputs=external)
    return document


def _proposal_digest(output):
    # Temporary proposal and finalized framework live in different directories.
    # Numerical output, scenarios, source digests and all other fields remain
    # bound; only machine-specific path/runtime observations are excluded.
    return hashlib.sha256(_json_bytes(_proposal_projection(output))).hexdigest()


def _proposal_projection(output):
    # Peer digests include paths; peer values and source digests stay bound.
    stable = {key: value for key, value in output.items()
              if key not in ('source_paths', 'root_identity', 'runtime', 'peer_result_digests')}
    if 'peer_snapshots' in stable:
        stable['peer_snapshots'] = {alias: _proposal_projection(peer)
                                    for alias, peer in stable['peer_snapshots'].items()}
    return stable


def prepare(service, draft):
    """Freeze the proposed result and current pointer, without publishing it."""
    document = _validated(draft)
    ticker = document['output']['ticker']
    if not service.path.exists():
        version = None
    else:
        with service._db() as con:
            version = service._version(con, ticker)
    return {'ticker': ticker, 'proposal_digest': _proposal_digest(document['output']),
            'expected_version': version, 'database': str(service.path.resolve())}


def _checked_receipt(draft, receipt, database):
    if not isinstance(receipt, dict) or set(receipt) != {'ticker', 'proposal_digest', 'expected_version', 'database'}:
        raise ValueError('proposal receipt is required')
    document = _validated(draft)
    ticker = document['output']['ticker']
    if receipt['ticker'] != ticker or receipt['proposal_digest'] != _proposal_digest(document['output']):
        raise ValueError('final result differs from the proposed result')
    if receipt['database'] != str(Path(database).resolve()):
        raise ValueError('result database differs from the prepared proposal')
    expected = receipt['expected_version']
    if expected is not None and (not isinstance(expected, str) or not expected):
        raise ValueError('invalid prior result version')
    return document, ticker, expected


def _formal_roots(document, code_root, content_root):
    expected = {}
    for name, value in (('code', code_root), ('content', content_root)):
        path = Path(value)
        if not path.is_absolute() or '..' in path.parts:
            raise ValueError('formal roots must be absolute directories')
        resolved = path.resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError('formal roots must be directories')
        expected[name] = str(resolved)
    identity = document['output']['root_identity']
    if any(identity[f'{name}_resolved'] != target for name, target in expected.items()):
        raise ValueError('final result was not calculated from the formal roots')


def _check_current_sources(service, ticker, draft, generation, source_provider_factory=None):
    document = _validated(draft)
    requires_external = document['output'].get('external_inputs_digest') is not None
    observed = service if source_provider_factory is None else SimpleNamespace(provider=source_provider_factory())
    _, source_status = _source_observation(observed, ticker, draft, generation)
    try:
        direct_status = observed.provider.source_status(ticker, draft)
    except Exception:  # noqa: BLE001 — unavailable is not proof of a change
        direct_status = 'unavailable'
    if direct_status == 'changed':
        raise ValueError('source changed after the proposal')
    inspect = getattr(observed.provider, 'source_conditions', None)
    if callable(inspect):
        try:
            conditions = inspect(ticker, draft)
        except Exception:  # noqa: BLE001 — unavailable is not proof of a change
            if requires_external:
                raise ValueError('external history source unavailable') from None
            conditions = ()
        if conditions is None:
            raise ValueError('result source snapshot is invalid')
        if not isinstance(conditions, (tuple, list)):
            raise ValueError('result source conditions are invalid')
        if any(flag in ('valuation_source_changed', 'external_history_source_changed') for flag in conditions):
            raise ValueError('source changed after the proposal')
        if requires_external and 'external_history_source_unavailable' in conditions:
            raise ValueError('external history source unavailable')
    elif requires_external:
        raise ValueError('external history source unavailable')
    if source_status == 'changed':
        raise ValueError('source changed after the proposal')
    return source_status


def publish(service, archive, draft, receipt, authorization, *, code_root, content_root,
            source_provider_factory=None):
    """Record an explicitly attested conversation decision in the shared store."""
    if not isinstance(authorization, ConversationAuthorization):
        raise ValueError('explicit conversation authorization is required')
    if not isinstance(archive, FrameworkArchive):
        raise ValueError('shared archive is required')
    document, ticker, expected = _checked_receipt(draft, receipt, service.path)
    _formal_roots(document, code_root, content_root)
    status, record = _capture(service, ticker, Captured(draft, None), None)
    if status != 'ready' or record is None:
        raise ValueError('result snapshot is unavailable or invalid')
    generation, captured, raw, hashes, historical, entry, observations = record
    batch_id, version = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    accepted_at = datetime.now(timezone.utc).isoformat()
    operator = getpass.getuser()
    if not operator:
        raise ValueError('local operator identity is unavailable')
    with archive.locked() as archived:
        if ticker in archived:
            raise ValueError('archived ticker cannot be published')
        # A rejected first publication must not initialize the store. Keep the
        # archive lock across this preflight, initialization and final commit.
        if service._read_only:
            if service.path.exists():
                with service._db() as existing:
                    if service._version(existing, ticker) != expected:
                        raise RevisionConflict('published result changed since proposal')
            elif expected is not None:
                raise RevisionConflict('published result changed since proposal')
            _check_current_sources(service, ticker, captured.draft, generation, source_provider_factory)
            from lynch_reader.valuation_reviews import ReviewService
            service = ReviewService(service.path, service.provider, service.devices)
        with service._db() as con:
            con.execute('BEGIN IMMEDIATE')
            if service._version(con, ticker) != expected:
                raise RevisionConflict('published result changed since proposal')
            source_status = _check_current_sources(service, ticker, captured.draft, generation, source_provider_factory)
            observations.append({'stage': 'commit', 'status': source_status, 'observed_at': accepted_at})
            digest = _hash(raw)
            item = {'status': 'accepted', 'version': version, 'generation': generation,
                    'snapshot_digest': digest, 'source_digests': hashes, 'migration_script_digest': hashes[entry],
                    'output_digest': _hash(_json_bytes(captured.draft.output)), 'historical_evidence': historical,
                    'source_observations': observations, 'replaces_version': expected, 'execution_ref': None}
            manifest = {'format': 1, 'origin': 'local_operator_attestation', 'purpose': 'conversation_update',
                        'operator': operator, 'session_reference': authorization.session_reference,
                        'user_authorization': authorization.user_authorization, 'scope': [ticker],
                        'archived': sorted(archived), 'accepted_at': accepted_at, 'items': {ticker: item},
                        'proposal_digest': receipt['proposal_digest']}
            encoded = _json_bytes(manifest)
            con.execute('INSERT INTO legacy_batches VALUES (?,?,?,?)', (batch_id, encoded, _hash(encoded), accepted_at))
            con.execute('INSERT INTO legacy_items VALUES (?,?,?,?,?,?,?)',
                        (version, ticker, batch_id, generation, raw, digest, None))
            if expected is None:
                con.execute('INSERT INTO results VALUES (?,?,?)', (ticker, version, 'legacy:' + batch_id))
            else:
                updated = con.execute('UPDATE results SET version=?,review_id=? WHERE ticker=? AND version=?',
                                      (version, 'legacy:' + batch_id, ticker, expected))
                if updated.rowcount != 1:
                    raise RevisionConflict('published result changed during update')
            con.commit()
    return {'status': 'local_accepted', 'origin': 'local_operator_attestation',
            'batch_id': batch_id, 'version': version}


def _absolute_file(value):
    path = Path(value)
    if not path.is_absolute() or not path.is_file():
        raise ValueError('an existing absolute input path is required')
    return path


def _database_path(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or (path.exists() and not path.is_file()):
        raise ValueError('database must be an absolute file path')
    configured = os.environ.get('LYNCH_VALUATION_DB')
    if not configured:
        raise ValueError('LYNCH_VALUATION_DB must name the app result database')
    expected = Path(configured)
    if not expected.is_absolute() or path.resolve() != expected.resolve():
        raise ValueError('database does not match the app result database')
    return path


def _read_json(path):
    with _absolute_file(path).open('rb') as stream:
        raw = stream.read(MAX_RECORD_BYTES + 1)
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError('input is too large')
    return _load_json(raw)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    for operation in ('prepare', 'publish'):
        command = sub.add_parser(operation)
        command.add_argument('--database', required=True)
        command.add_argument('--result', required=True)
        command.add_argument('--external-inputs')
        if operation == 'publish':
            command.add_argument('--archive', required=True)
            command.add_argument('--receipt', required=True)
            command.add_argument('--session-reference', required=True)
            command.add_argument('--authorization', required=True)
            command.add_argument('--code-root', required=True)
            command.add_argument('--content-root', required=True)
    args = parser.parse_args(argv)
    database = _database_path(args.database)
    result_path = _absolute_file(args.result)
    if database.resolve() == result_path.resolve():
        raise ValueError('result path overlaps the database')
    output = _read_json(result_path)
    external = _read_json(args.external_inputs) if args.external_inputs else None
    draft = draft_from_result(output, external_inputs=external)
    from eval.lib.result_service import _configured_sources
    from lynch_reader.valuation_reviews import ReviewService
    if args.operation == 'prepare':
        service = ReviewService.open_readonly(database, _configured_sources())
        response = prepare(service, draft)
    else:
        supplied_archive = Path(args.archive)
        if (not supplied_archive.is_absolute() or supplied_archive.resolve() != ARCHIVE_FILE.resolve()
                or (supplied_archive.exists() and not supplied_archive.is_file())):
            raise ValueError('archive does not match the app archive file')
        archive = ARCHIVE_FILE
        if archive.exists() and not archive.is_file():
            raise ValueError('archive must be an absolute file path')
        receipt = _absolute_file(args.receipt)
        if len({database.resolve(), result_path.resolve(), archive.resolve(), receipt.resolve()}) != 4:
            raise ValueError('input paths overlap')
        # The app's archive store treats a not-yet-created list as empty.
        archived = _read_json(archive) if archive.exists() else []
        if not isinstance(archived, list) or any(not isinstance(item, str) for item in archived):
            raise ValueError('invalid archive list')
        authorization = ConversationAuthorization(args.session_reference, args.authorization)
        prepared = _read_json(receipt)
        document, _, _ = _checked_receipt(draft, prepared, database)
        code_arg, content_arg = Path(args.code_root), Path(args.content_root)
        if any(not path.is_absolute() or '..' in path.parts for path in (code_arg, content_arg)):
            raise ValueError('formal roots must be absolute directories')
        code_root, content_root = code_arg.resolve(strict=True), content_arg.resolve(strict=True)
        installed_code = Path(__file__).resolve().parents[3]
        installed_content = (installed_code / 'frameworks').resolve(strict=True)
        if code_root != installed_code or content_root != installed_content:
            raise ValueError('formal roots do not match the installed app worktree')
        _formal_roots(document, code_root, content_root)
        service = ReviewService.open_readonly(database, _configured_sources())
        response = publish(service, FrameworkArchive(archive), draft, prepared, authorization,
                           code_root=code_root, content_root=content_root, source_provider_factory=_configured_sources)
    print(_json_bytes(response).decode())
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        print(f'FAIL: {exc}', file=sys.stderr)
        raise SystemExit(1)
