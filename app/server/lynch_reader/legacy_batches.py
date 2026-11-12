"""Local operator attestation for retaining historical migration results.

This is not proof of a user's identity or a simulated per-ticker device click.
Only trusted local code may call it; there is deliberately no HTTP endpoint.
"""
from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import getpass
import hashlib
import json
import math
from pathlib import Path
import re
import secrets

from eval.lib.valuation_drafts import Draft, _json_bytes, _load_json, _validate_draft


HISTORICAL_NOTE_FLAGS = frozenset({
    'historical_reference_only',
    'historical_reference_double_counting',
    'historical_candidate_upgrade_pending',
    'historical_low_display_difference',
    'historical_option_cost_basis_difference',
    'historical_option_mid_high_two_decimals',
    'historical_conditional_funding',
    'historical_mid_forecast_weakened',
    'historical_base_two_decimals',
    'historical_financial_basis_unknown',
    'historical_base_four_decimals',
    'historical_option_all_two_decimals',
    'historical_candidate_reference_conflict',
    'current_framework_funding_gap_unquantified',
    'historical_option_mid_high_one_decimal',
    'historical_separate_syntax_error',
    'historical_earlier_failed_log',
    'historical_run_log_incomplete',
})


@dataclass(frozen=True)
class LocalBatchAuthorization:
    """An operator's explicit statement of an existing user authorization.

    The calling operator is responsible for verifying the cited real message,
    scope and archived list. This object does not claim independent proof.
    """
    session_reference: str
    user_authorization: str
    workline_reference: str
    scope: tuple[str, ...]

    def __post_init__(self):
        if not all(isinstance(value, str) and value.strip() for value in
                   (self.session_reference, self.user_authorization, self.workline_reference)):
            raise ValueError('local operator must cite the user message and workline')
        if (not isinstance(self.scope, tuple) or not self.scope or len(set(self.scope)) != len(self.scope)
                or any(not isinstance(ticker, str) or not re.fullmatch(r'[A-Z0-9]{1,10}\.(?:US|HK|SH|SZ|SG)', ticker)
                       for ticker in self.scope)):
            raise ValueError('scope must contain unique ticker identifiers')


@dataclass(frozen=True)
class HistoricalEvidence:
    """Operator-selected historical result and original script; never executed here."""
    result_path: Path
    script_path: Path
    rationale: str
    manifest_path: Path | None = None


def _valuation_projection(output):
    """Compare every displayed price band and disclosed option amount exactly."""
    if not isinstance(output, dict) or not isinstance(output.get('ticker'), str) or not isinstance(output.get('currency'), str):
        raise ValueError('historical result identity missing')
    def band(value):
        if not isinstance(value, dict) or not {'L', 'M', 'H'} <= set(value):
            raise ValueError('historical band missing')
        values = {key: value[key] for key in ('L', 'M', 'H')}
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
               for value in values.values()):
            raise ValueError('invalid historical band')
        return values
    candidate = output.get('candidate')
    option = output.get('option')
    if candidate is not None:
        if isinstance(candidate, dict) and candidate.get('status') == 'unavailable':
            candidate = {'status': 'unavailable', 'label': candidate.get('label')}
        else:
            if not isinstance(candidate, dict) or not isinstance(candidate.get('label'), str):
                raise ValueError('invalid historical candidate')
            candidate = {'label': candidate['label'], 'anchors': band(candidate['anchors'])}
    if option is not None:
        if not isinstance(option, dict):
            raise ValueError('invalid historical option')
        value = option.get('value')
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
            raise ValueError('invalid historical option value')
        option = ({'status': 'unavailable', 'value': value, 'value_unit': option.get('value_unit')}
                  if option.get('status') == 'unavailable' else
                  {'anchors': band(option['anchors']), 'value': value, 'value_unit': option.get('value_unit')})
    return {'ticker': output['ticker'], 'currency': output['currency'], 'anchors': band(output['anchors']),
            'candidate': candidate, 'option': option}


def _historical_matches(result_bytes, kind, output, manifest_bytes=None):
    """Compatibility predicate for one-time inspection; never used by current reads."""
    from lynch_reader.historical_comparison import EvidenceMismatch, compare
    try:
        result = compare(result_bytes, kind, output, manifest_bytes)
    except EvidenceMismatch:
        return False
    return not result['differences'] and not any(flag.endswith('_unknown') for flag in result['uncertainties'])


def _historical_evidence(evidence, output):
    from lynch_reader.valuation_reviews import MAX_SNAPSHOT_BYTES
    if not isinstance(evidence, HistoricalEvidence):
        raise TypeError('historical evidence is required')
    if not isinstance(evidence.rationale, str) or not evidence.rationale.strip():
        raise ValueError('historical evidence rationale is required')
    paths = (Path(evidence.result_path), Path(evidence.script_path))
    if any(not path.is_absolute() or not path.is_file() for path in paths):
        raise ValueError('historical evidence paths must be existing absolute files')
    if paths[1].name != output['ticker'] + '.py':
        raise ValueError('historical script ticker does not match result')
    result_bytes, script_bytes = (path.read_bytes() for path in paths)
    if not result_bytes or not script_bytes or max(len(result_bytes), len(script_bytes)) > MAX_SNAPSHOT_BYTES:
        raise ValueError('historical evidence is empty or too large')
    kind = {'.json': 'json', '.md': 'markdown'}.get(paths[0].suffix.lower())
    manifest_path = Path(evidence.manifest_path) if evidence.manifest_path is not None else None
    if manifest_path is not None and (not manifest_path.is_absolute() or not manifest_path.is_file()):
        raise ValueError('operator manifest must be an existing absolute file')
    manifest_bytes = manifest_path.read_bytes() if manifest_path is not None else None
    if manifest_bytes is not None and (not manifest_bytes or len(manifest_bytes) > MAX_SNAPSHOT_BYTES):
        raise ValueError('operator manifest is empty or too large')
    from lynch_reader.historical_comparison import EvidenceMismatch, compare
    try:
        comparison = compare(result_bytes, kind, output, manifest_bytes)
    except EvidenceMismatch as exc:
        raise ArithmeticError(str(exc)) from exc
    return {
        'result_path': str(paths[0]), 'result_sha256': _hash(result_bytes),
        'result_bytes': base64.b64encode(result_bytes).decode('ascii'),
        'script_path': str(paths[1]), 'script_sha256': _hash(script_bytes),
        'script_bytes': base64.b64encode(script_bytes).decode('ascii'),
        'rationale': evidence.rationale,
        'verification': ('historical_unverified' if 'historical_material_unreviewed' in comparison['uncertainties']
                         else 'historical_comparison_only'), 'kind': kind,
        'compared_projection_digest': _hash(_json_bytes(_valuation_projection(output))),
        'comparison': comparison,
        'manifest_path': str(manifest_path) if manifest_path is not None else None,
        'manifest_sha256': _hash(manifest_bytes) if manifest_bytes is not None else None,
        'manifest_bytes': base64.b64encode(manifest_bytes).decode('ascii') if manifest_bytes is not None else None,
    }


def _unreviewed_evidence(evidence, output, reason='historical_material_unreviewed', saved=None):
    """Freeze available bytes without claiming they were successfully interpreted."""
    from lynch_reader.historical_comparison import unreviewed
    from lynch_reader.valuation_reviews import MAX_SNAPSHOT_BYTES
    record = dict(saved) if saved is not None else {}
    for role, attribute in (('result', 'result_path'), ('script', 'script_path'), ('manifest', 'manifest_path')):
        if saved is not None:
            continue
        raw_path = getattr(evidence, attribute, None)
        record[role + '_path'] = str(raw_path) if isinstance(raw_path, Path) and raw_path.is_absolute() else None
        record[role + '_bytes'], record[role + '_sha256'] = None, None
        if record[role + '_path'] is None:
            continue
        try:
            payload = Path(record[role + '_path']).read_bytes()
            if len(payload) <= MAX_SNAPSHOT_BYTES:
                record[role + '_bytes'] = base64.b64encode(payload).decode('ascii')
                record[role + '_sha256'] = _hash(payload)
        except OSError:
            pass
    rationale = getattr(evidence, 'rationale', None)
    record['rationale'] = rationale if isinstance(rationale, str) and rationale.strip() else '历史资料待核'
    record['verification'] = 'historical_unverified'
    record['kind'] = None
    record['comparison'] = unreviewed(output, reason)
    record['compared_projection_digest'] = _hash(_json_bytes(_valuation_projection(output)))
    return record


def _historical_evidence_current(evidence):
    return all(evidence.get(role + '_sha256') is None or
               _hash(Path(evidence[role + '_path']).read_bytes()) == evidence[role + '_sha256']
               for role in ('result', 'script', 'manifest'))


def create_tables(con):
    con.executescript('''
        CREATE TABLE IF NOT EXISTS legacy_batches (
            id TEXT PRIMARY KEY, manifest BLOB NOT NULL, digest TEXT NOT NULL, accepted_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS legacy_items (
            version TEXT PRIMARY KEY, ticker TEXT NOT NULL, batch_id TEXT NOT NULL,
            generation TEXT NOT NULL, document BLOB NOT NULL, digest TEXT NOT NULL, execution_ref TEXT
        );
        CREATE TRIGGER IF NOT EXISTS immutable_legacy_batch_update BEFORE UPDATE ON legacy_batches
            BEGIN SELECT RAISE(ABORT, 'immutable legacy batch'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_legacy_batch_delete BEFORE DELETE ON legacy_batches
            BEGIN SELECT RAISE(ABORT, 'immutable legacy batch'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_legacy_item_update BEFORE UPDATE ON legacy_items
            BEGIN SELECT RAISE(ABORT, 'immutable legacy item'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_legacy_item_delete BEFORE DELETE ON legacy_items
            BEGIN SELECT RAISE(ABORT, 'immutable legacy item'); END;
    ''')


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _historical_saved_output(data, ticker):
    """Check a saved historical output without asserting a script was run."""
    runtime = data.get('runtime')
    if not isinstance(runtime, dict) or runtime.get('source') != 'historical_saved_output':
        return None
    if runtime.get('execution_verified') is not False or not isinstance(runtime.get('script_missing'), bool):
        raise ValueError('invalid historical output provenance')
    if runtime.get('source_status') not in (None, 'recorded_source_changed'):
        raise ValueError('invalid historical source status')
    notes = runtime.get('history_notes', [])
    if (not isinstance(notes, list) or any(not isinstance(note, str) or note not in HISTORICAL_NOTE_FLAGS for note in notes)
            or len(set(notes)) != len(notes)):
        raise ValueError('invalid historical notes')
    output_name = f'historical/{ticker}.json'
    script_name = f'historical/{ticker}.py'
    missing = runtime['script_missing']
    script_status = runtime.get('script_status')
    if (not missing and script_status != 'verified') or (missing and script_status == 'verified'):
        raise ValueError('historical script status conflicts with association')
    expected = {output_name} if missing else {output_name, script_name}
    if set(data['inputs']) != expected or data['entry'] != (output_name if missing else script_name):
        raise ValueError('historical output artifacts do not match provenance')
    raw = base64.b64decode(data['inputs'][output_name], validate=True)
    if _load_json(raw) != data['output']:
        raise ValueError('saved output differs from original historical output')
    if runtime.get('result_sha256') is not None and runtime['result_sha256'] != _hash(raw):
        raise ValueError('historical output digest differs from its bytes')
    if not missing:
        script = base64.b64decode(data['inputs'][script_name], validate=True)
        if runtime.get('script_sha256') != _hash(script):
            raise ValueError('historical script digest differs from its bytes')
    return missing


def _source_observation(service, ticker, draft, generation=None):
    """Observe mutable sources separately from the frozen snapshot's integrity."""
    changed = False
    unavailable = False
    observed_generation = ''
    frozen_method = getattr(service.provider, 'frozen_generation', None)
    frozen_generation = None
    if callable(frozen_method):
        try:
            frozen_generation = frozen_method(ticker, draft)
            if not isinstance(frozen_generation, str) or not frozen_generation:
                raise ValueError('frozen generation unavailable')
        except Exception:  # noqa: BLE001 — valid snapshot can outlive its version reader
            unavailable = True
    try:
        observed_generation = service.provider.generation(ticker)
        if not isinstance(observed_generation, str) or not observed_generation:
            raise ValueError('source generation unavailable')
        if generation is not None:
            reference = frozen_generation if callable(frozen_method) else generation
            if not reference:
                unavailable = True
            elif callable(getattr(service.provider, 'generation_matches', None)):
                changed |= not service.provider.generation_matches(ticker, draft)
            elif observed_generation != reference:
                changed = True
    except Exception:  # noqa: BLE001 — source checks do not invalidate a frozen result
        observed_generation = ''
        unavailable = True
    try:
        currency = service.provider.currency(ticker)
        if not isinstance(currency, str) or not currency:
            raise ValueError('source currency unavailable')
        if currency != draft.output['currency']:
            changed = True
    except Exception:  # noqa: BLE001
        unavailable = True
    try:
        status = service.provider.source_status(ticker, draft)
        if status not in ('match', 'changed', 'unavailable'):
            raise ValueError('invalid source status')
        changed |= status == 'changed'
        unavailable |= status == 'unavailable'
    except Exception:  # noqa: BLE001
        unavailable = True
    # A partial check cannot establish a current change or authorize replacement.
    status = 'unavailable' if unavailable else 'changed' if changed else 'match'
    snapshot_generation = frozen_generation if frozen_generation is not None else (observed_generation if status == 'match' else '')
    return snapshot_generation, status


def _capture(service, ticker, captured, evidence):
    from lynch_reader.valuation_reviews import Captured, Problem, _check_units
    if captured is None:
        return 'missing', None
    try:
        if not isinstance(captured, Captured):
            raise ValueError('invalid capture')
        if captured.execution_ref is not None and not isinstance(captured.execution_ref, str):
            raise ValueError('invalid execution reference')
        if not isinstance(captured.draft, Draft):
            raise ValueError('invalid draft')
        document = captured.draft._document
        from lynch_reader.valuation_reviews import MAX_SNAPSHOT_BYTES
        if len(document) > MAX_SNAPSHOT_BYTES:
            raise ValueError('snapshot too large')
        data = _load_json(document)
        _validate_draft(data)
        if data['output']['ticker'] != ticker:
            raise ValueError('capture identity mismatch')
        historical_missing = _historical_saved_output(data, ticker)
        if historical_missing is None and Path(data['entry']).name != ticker + '.py':
            raise ValueError('migration script ticker does not match result')
        _check_units(data['output'])
        generation, source_status = _source_observation(service, ticker, captured.draft)
        source_observations = [{'stage': 'capture', 'status': source_status,
                                'observed_at': datetime.now(timezone.utc).isoformat()}]
        try:
            historical = _historical_evidence(evidence, data['output'])
        except Exception:  # noqa: BLE001 — unusable history must not discard a valid script result
            historical = _unreviewed_evidence(evidence, data['output'])
        if historical_missing is not None and evidence is None:
            result_path = data['runtime'].get('result_path')
            if isinstance(result_path, str) and Path(result_path).is_absolute():
                # Original bytes are already frozen in the draft; this is a
                # source reference, not an independent comparison.
                historical['result_path'] = result_path
                historical['rationale'] = '沿用保存的历史输出，尚未核对现行脚本及执行来源'
        source_hashes = {key: _hash(base64.b64decode(value, validate=True))
                         for key, value in data['inputs'].items()}
        return 'ready', (generation, captured, document, source_hashes, historical, data['entry'], source_observations)
    except KeyError:
        return 'missing', None
    except (Problem, ValueError, TypeError, OSError, AttributeError):
        return 'invalid', None
    except Exception:  # noqa: BLE001 — one provider failure must not abort the batch
        return 'unavailable', None


def _same_authorization(service, ticker, version, authorization):
    with service._db() as con:
        row = con.execute('SELECT b.manifest,b.digest FROM legacy_items i '
                          'JOIN legacy_batches b ON i.batch_id=b.id WHERE i.ticker=? AND i.version=?',
                          (ticker, version)).fetchone()
    if row is None:
        return False
    try:
        encoded = bytes(row[0])
        if not secrets.compare_digest(_hash(encoded), row[1]):
            return False
        manifest = _load_json(encoded)
        return (manifest['session_reference'] == authorization.session_reference
                and manifest['user_authorization'] == authorization.user_authorization
                and manifest['workline_reference'] == authorization.workline_reference
                and ticker in manifest['scope'])
    except (ValueError, TypeError, KeyError):
        return False


def accept(service, authorization, *, archive, snapshots, evidence):
    """Record supplied frozen snapshots; never executes a model or replaces an existing result."""
    if not isinstance(authorization, LocalBatchAuthorization):
        raise TypeError('structured local operator authorization is required')
    from lynch_reader.archive import FrameworkArchive
    if not isinstance(archive, FrameworkArchive):
        raise ValueError('the shared framework archive is required')
    scope = sorted(authorization.scope)
    if not isinstance(snapshots, Mapping) or any(ticker not in scope for ticker in snapshots):
        raise ValueError('snapshots must be an explicitly scoped mapping')
    if not isinstance(evidence, Mapping) or any(ticker not in scope for ticker in evidence):
        raise ValueError('historical evidence must be an explicitly scoped mapping')
    with service._db() as con:
        expected = {ticker: con.execute('SELECT version,review_id FROM results WHERE ticker=?', (ticker,)).fetchone()
                    for ticker in scope}
    replaceable = {ticker for ticker, pointer in expected.items()
                   if pointer is not None and pointer[1].startswith('legacy:')
                   and current(service, ticker, pointer[0]).get('source_changed_now') is True}
    matching_authorization = {ticker for ticker in replaceable
                              if _same_authorization(service, ticker, expected[ticker][0], authorization)}
    initially_archived = archive.tickers()
    prepared = {}
    for ticker in scope:
        if ticker in initially_archived or (expected[ticker] is not None and ticker not in matching_authorization):
            continue
        try:
            prepared[ticker] = _capture(service, ticker, snapshots.get(ticker), evidence.get(ticker))
        except Exception:  # noqa: BLE001 — isolate malformed per-ticker input mappings
            prepared[ticker] = ('unavailable', None)
    batch_id = secrets.token_urlsafe(24)
    accepted_at = datetime.now(timezone.utc).isoformat()
    operator = getpass.getuser()
    if not operator:
        raise ValueError('local operator identity is unavailable')
    results = {}
    records = []
    manifest_items = {}
    # Archive updates and this SQLite commit share the same cross-process lock.
    with archive.locked() as archived, service._db() as con:
        con.execute('BEGIN IMMEDIATE')
        for ticker in scope:
            if ticker in archived:
                results[ticker] = 'archived'
                manifest_items[ticker] = {'status': 'archived'}
                continue
            actual = con.execute('SELECT version,review_id FROM results WHERE ticker=?', (ticker,)).fetchone()
            if actual != expected[ticker]:
                # Another transaction won; do not replace its pointer. A retry
                # can decide whether its current result is eligible to replace.
                status = 'existing' if actual is not None else 'changed'
                results[ticker] = status
                manifest_items[ticker] = {'status': status}
                continue
            if ticker in replaceable and ticker not in matching_authorization:
                results[ticker] = 'authorization_mismatch'
                manifest_items[ticker] = {'status': 'authorization_mismatch'}
                continue
            if actual is not None and (ticker not in replaceable
                                       or current(service, ticker, actual[0]).get('source_changed_now') is not True):
                results[ticker] = 'existing'
                manifest_items[ticker] = {'status': 'existing'}
                continue
            # If an existing pointer disappeared while capture ran, retry in a
            # future batch instead of inventing a result without a snapshot.
            status, record = prepared.get(ticker, ('changed', None))
            if record is not None:
                generation, captured, document, hashes, historical, entry, source_observations = record
                _, commit_status = _source_observation(service, ticker, captured.draft, generation)
                source_observations.append({'stage': 'commit', 'status': commit_status,
                                            'observed_at': datetime.now(timezone.utc).isoformat()})
                # A definite change in the new snapshot's inputs must not
                # replace a previously displayed result, even when other
                # sources or the runtime cannot be verified.
                inspect = getattr(service.provider, 'source_conditions', None)
                if actual is not None and callable(inspect):
                    try:
                        conditions = inspect(ticker, captured.draft)
                    except Exception:  # noqa: BLE001 — source access may be unavailable
                        conditions = ()
                    if conditions is None or any(flag in ('valuation_source_changed', 'external_history_source_changed')
                                                 for flag in conditions):
                        results[ticker] = 'source_changed' if conditions is not None else 'invalid'
                        manifest_items[ticker] = {'status': results[ticker]}
                        continue
                try:
                    history_current = _historical_evidence_current(historical)
                except Exception:  # noqa: BLE001 — a missing history file is not a missing result
                    history_current = False
                if not history_current:
                    historical = _unreviewed_evidence(evidence.get(ticker), captured.draft.output,
                                                      'historical_source_changed', saved=historical)
            if record is None:
                results[ticker] = status
                manifest_items[ticker] = {'status': status}
                continue
            version = secrets.token_urlsafe(24)
            digest = _hash(document)
            results[ticker] = 'replaced' if actual is not None else 'accepted'
            manifest_items[ticker] = {'status': 'accepted', 'version': version, 'generation': generation,
                                      'snapshot_digest': digest, 'source_digests': hashes,
                                      ('source_artifact_digest' if _historical_saved_output(_load_json(document), ticker) is True
                                       else 'migration_script_digest'): hashes[entry],
                                      'output_digest': _hash(_json_bytes(captured.draft.output)),
                                      'historical_evidence': historical,
                                      'source_observations': source_observations,
                                      'replaces_version': actual[0] if actual is not None else None,
                                      'execution_ref': captured.execution_ref}
            records.append(((version, ticker, batch_id, generation, document, digest, captured.execution_ref), actual))
        if not records:
            # A no-op batch must not mutate even the audit tables; the caller
            # receives the per-ticker outcomes and can retry selectively.
            con.rollback()
            return {'batch_id': None, 'origin': 'local_operator_attestation',
                    'accepted_at': accepted_at, 'items': results}
        manifest = {
            'format': 1, 'origin': 'local_operator_attestation', 'operator': operator,
            'session_reference': authorization.session_reference,
            'user_authorization': authorization.user_authorization,
            'workline_reference': authorization.workline_reference,
            'scope': scope, 'archived': sorted(archived), 'accepted_at': accepted_at,
            'items': manifest_items,
        }
        encoded = _json_bytes(manifest)
        con.execute('INSERT INTO legacy_batches VALUES (?,?,?,?)', (batch_id, encoded, _hash(encoded), accepted_at))
        for record, previous in records:
            con.execute('INSERT INTO legacy_items VALUES (?,?,?,?,?,?,?)', record)
            if previous is None:
                con.execute('INSERT INTO results VALUES (?,?,?)', (record[1], record[0], f'legacy:{batch_id}'))
            else:
                updated = con.execute('UPDATE results SET version=?,review_id=? WHERE ticker=? AND version=? AND review_id=?',
                                      (record[0], f'legacy:{batch_id}', record[1], previous[0], previous[1]))
                if updated.rowcount != 1:
                    raise ValueError('legacy result pointer changed during commit')
        con.commit()
    return {'batch_id': batch_id, 'origin': 'local_operator_attestation', 'accepted_at': accepted_at, 'items': results}


def current(service, ticker, expected_version):
    """Read an accepted historical result, checking its event and current source."""
    from lynch_reader.valuation_reviews import Problem, _check_units
    with service._db() as con:
        row = con.execute(
            'SELECT i.version,i.ticker,i.batch_id,i.generation,i.document,i.digest,i.execution_ref, '
            'b.manifest,b.digest,b.accepted_at FROM legacy_items i JOIN legacy_batches b ON i.batch_id=b.id '
            'JOIN results r ON r.ticker=i.ticker AND r.version=i.version AND r.review_id=(\'legacy:\' || i.batch_id) '
            'WHERE i.ticker=? AND i.version=?', (ticker, expected_version)).fetchone()
    if row is None:
        return {'status': 'invalid'}
    try:
        version, identity, batch_id, generation, document, digest, execution_ref, encoded, batch_digest, accepted_at = row
        document, encoded = bytes(document), bytes(encoded)
        if not secrets.compare_digest(_hash(document), digest) or not secrets.compare_digest(_hash(encoded), batch_digest):
            return {'status': 'invalid'}
        manifest, data = _load_json(encoded), _load_json(document)
        _validate_draft(data)
        item = manifest['items'][ticker]
        purpose = manifest.get('purpose', 'historical_migration')
        if purpose not in ('historical_migration', 'conversation_update'):
            return {'status': 'invalid'}
        if purpose == 'conversation_update':
            from lynch_reader.conversation_publication import _proposal_digest
            if (not isinstance(manifest.get('session_reference'), str) or not manifest['session_reference'].strip()
                    or not isinstance(manifest.get('user_authorization'), str) or not manifest['user_authorization'].strip()
                    or manifest.get('proposal_digest') != _proposal_digest(data['output'])):
                return {'status': 'invalid'}
        if (identity != ticker or data['output']['ticker'] != ticker or manifest['origin'] != 'local_operator_attestation'
                or item['status'] != 'accepted' or item['version'] != version or item['generation'] != generation
                or item['snapshot_digest'] != digest or item['execution_ref'] != execution_ref
                or manifest['accepted_at'] != accepted_at or ticker not in manifest['scope']):
            return {'status': 'invalid'}
        hashes = {key: _hash(base64.b64decode(value, validate=True)) for key, value in data['inputs'].items()}
        if item['source_digests'] != hashes:
            return {'status': 'invalid'}
        historical = item['historical_evidence']
        if historical['verification'] not in ('historical_comparison_only', 'historical_unverified'):
            return {'status': 'invalid'}
        for role in ('result', 'script', 'manifest'):
            raw, digest_value = historical.get(role + '_bytes'), historical.get(role + '_sha256')
            if (raw is None) != (digest_value is None):
                return {'status': 'invalid'}
            if raw is not None and _hash(base64.b64decode(raw, validate=True)) != digest_value:
                return {'status': 'invalid'}
        if (historical['verification'] == 'historical_comparison_only'
                and (historical.get('result_bytes') is None or historical.get('script_bytes') is None)):
            return {'status': 'invalid'}
        historical_missing = _historical_saved_output(data, ticker)
        entry_digest = 'source_artifact_digest' if historical_missing is True else 'migration_script_digest'
        if (item[entry_digest] != hashes[data['entry']]
                or item['output_digest'] != _hash(_json_bytes(data['output']))):
            return {'status': 'invalid'}
        # Historical prose is parsed once during import. Runtime readers use
        # only the accepted snapshot plus the recorded comparison digest.
        if historical['compared_projection_digest'] != _hash(_json_bytes(_valuation_projection(data['output']))):
            return {'status': 'invalid'}
        timestamp = datetime.fromisoformat(accepted_at)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            return {'status': 'invalid'}
        _check_units(data['output'])
        if historical_missing is None and Path(data['entry']).name != ticker + '.py':
            return {'status': 'invalid'}
        saved_flags = item.get('source_observation_flags', [])
        if (not isinstance(saved_flags, list) or any(flag not in
                ('valuation_source_unavailable', 'valuation_source_changed') for flag in saved_flags)):
            return {'status': 'invalid'}
        observations = item.get('source_observations', [])
        if not isinstance(observations, list):
            return {'status': 'invalid'}
        for observation in observations:
            if (not isinstance(observation, dict) or observation.get('stage') not in ('capture', 'commit')
                    or observation.get('status') not in ('match', 'changed', 'unavailable')):
                return {'status': 'invalid'}
            stamp = datetime.fromisoformat(observation['observed_at'])
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                return {'status': 'invalid'}
        draft = Draft(document)
        inspect = getattr(service.provider, 'source_conditions', None)
        detailed_flags = []
        if historical_missing is not None:
            # Old output + optional old script does not establish the current
            # model, its runtime, or that the output was independently run.
            detailed_flags = ['valuation_source_unavailable', 'valuation_runtime_unavailable']
            if historical_missing:
                detailed_flags.append('historical_script_missing')
            if data['runtime'].get('source_status') == 'recorded_source_changed':
                detailed_flags.append('historical_recorded_source_changed')
            detailed_flags.extend(note for note in data['runtime'].get('history_notes', [])
                                  if note not in ('historical_reference_only', 'historical_reference_double_counting'))
        elif callable(inspect):
            try:
                conditions = inspect(ticker, draft)
            except Exception:  # noqa: BLE001
                conditions = ('valuation_source_unavailable',)
            if conditions is None:
                return {'status': 'invalid'}
            detailed_flags = list(conditions)
        reference_generation = generation
        if (observations and observations[0]['status'] != 'match'
                and not callable(getattr(service.provider, 'frozen_generation', None))):
            # Older batches recorded the then-current generation even when it
            # did not match the snapshot; it is not evidence of snapshot identity.
            reference_generation = ''
        _, source_status = _source_observation(service, ticker, draft, reference_generation)
        if historical_missing is not None:
            source_status = 'unavailable'
        # Old flags were derived from a boolean that conflated changed and unreadable.
        past_statuses = {observation['status'] for observation in observations}
        if 'valuation_source_unavailable' in saved_flags:
            past_statuses.add('unavailable')
        past_flags = [f'valuation_source_previously_{status}' for status in ('unavailable', 'changed')
                      if status in past_statuses]
        if 'valuation_source_changed' in saved_flags:
            past_flags.append('valuation_source_previously_ambiguous')
        current_flags = [] if source_status == 'match' else [f'valuation_source_{source_status}']
        notes = data['runtime'].get('history_notes', []) if historical_missing is not None else []
        reference_double_counting = 'historical_reference_double_counting' in notes
        reference_only = 'historical_reference_only' in notes or reference_double_counting
        with service._db() as con:
            if service._version(con, ticker) != expected_version:
                return {'status': 'changed'}
            axis_supplement = None
            if purpose == 'historical_migration':
                try:
                    from eval.lib.axis_history import read_history
                    axis_supplement = read_history(con, ticker, expected_version)
                except Exception:  # noqa: BLE001 — damaged axis data must not hide a valid valuation
                    pass
        direct_source_change = any(flag in ('valuation_source_changed', 'external_history_source_changed')
                                   for flag in detailed_flags)
        flags = list(dict.fromkeys(historical['comparison']['uncertainties'] + past_flags + current_flags + detailed_flags))
        if purpose == 'conversation_update':
            flags = list(dict.fromkeys(['execution_unverified'] + current_flags + detailed_flags))
        return {'status': 'local_accepted' if purpose == 'conversation_update' else 'legacy_accepted',
                'origin': 'local_operator_attestation',
                'batch_id': batch_id, 'accepted_at': accepted_at,
                'verification': historical['verification'],
                'source_status': source_status, 'source_changed_now': source_status == 'changed' or direct_source_change,
                'source_observations': observations,
                'reference_only': reference_only, 'reference_double_counting': reference_double_counting,
                'historical_evidence_flags': flags,
                'historical_evidence_differences': historical['comparison']['differences'],
                'historical_evidence_source': {'reference': Path(historical['result_path']).name if historical.get('result_path') else '未提供历史资料',
                                               'rationale': historical['rationale']},
                'historical_comparison': historical['comparison'], 'output': data['output'],
                'axis_supplement': axis_supplement}
    except (Problem, ValueError, TypeError, KeyError, OSError, AttributeError):
        return {'status': 'invalid'}
    except Exception:  # noqa: BLE001 — report an unavailable provider, not a false current result
        return {'status': 'unavailable'}
