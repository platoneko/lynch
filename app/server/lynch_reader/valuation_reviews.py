"""Unwired valuation review routes. Formal confirmation is deliberately disabled.

The provider is server-owned, not supplied by HTTP. Its generation/current
checks detect declared changes only; they do not prove complete dependencies or
trusted execution. No production implementation of that proof exists yet.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from dataclasses import dataclass
import hashlib
import json
import math
import re
from pathlib import Path
import secrets
import sqlite3
import subprocess
from typing import Protocol

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from eval.lib.valuation_drafts import MAX_RECORD_BYTES, Draft, InvalidDraft, _load_json, _validate_draft
from lynch_reader.auth.gate import COOKIE, _read_at_most
from lynch_reader.auth.store import DeviceStore, StoreCorrupt
from lynch_reader.chat.routes import is_json

MAX_SNAPSHOT_BYTES = MAX_RECORD_BYTES


@dataclass(frozen=True)
class Captured:
    """Server-created calculation and an opaque runner reference, if any.

    The reference alone is never proof. A trusted built-in verifier must resolve
    it against its own immutable execution record and independently validate it.
    """
    draft: Draft
    execution_ref: str | None


class Provider(Protocol):
    def generation(self, ticker: str) -> str: ...
    def currency(self, ticker: str) -> str: ...
    def calculate(self, ticker: str) -> Captured: ...
    def current(self, ticker: str, draft: Draft) -> bool: ...


class Problem(Exception):
    def __init__(self, code: str, status: int) -> None:
        self.code, self.status = code, status


class ProofUnavailable(Problem):
    def __init__(self) -> None:
        super().__init__('proof_unavailable', 503)


class ExecutionVerifier(Protocol):
    def require_current(self, ticker: str, draft: Draft, execution_ref: str | None) -> None:
        """Resolve a server-owned execution record and independently verify it.

        A future implementation must verify its own receipt, frozen inputs,
        runtime, source generation, and trusted execution; a hash or a flag is
        insufficient. Failure or missing evidence must raise ProofUnavailable.
        """
        ...


class _UnavailableVerifier:
    def require_current(self, ticker: str, draft: Draft, execution_ref: str | None) -> None:
        raise ProofUnavailable()


def _builtin_execution_verifier() -> ExecutionVerifier:
    # Replace only with a reviewed, server-owned runner/verifier pair after
    # isolation, runtime and dependency closure are independently validated.
    return _UnavailableVerifier()


def _require_verified_execution(ticker: str, draft: Draft, execution_ref: str | None,
                                verifier: ExecutionVerifier | None = None) -> None:
    (verifier or _builtin_execution_verifier()).require_current(ticker, draft, execution_ref)


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check_units(output: dict) -> frozenset[str]:
    declared = []
    units = output.get('units')
    if units is not None:
        if not isinstance(units, dict):
            raise Problem('price_unit_unsupported', 422)
        if 'currency' in units and units['currency'] != output['currency']:
            raise Problem('price_unit_unsupported', 422)
        for key in ('price', 'anchors', 'candidate', 'option'):
            if key in units:
                declared.append(units[key])
    bands = [output.get('anchors')]
    for name in ('candidate', 'option'):
        band = output.get(name)
        if isinstance(band, dict):
            bands.extend((band, band.get('anchors')))
    for band in bands:
        if not isinstance(band, dict):
            continue
        for key in ('unit', 'units'):
            if key not in band:
                continue
            value = band[key]
            if key == 'units' and isinstance(value, dict):
                if set(value) != {'price'}:
                    raise Problem('price_unit_unsupported', 422)
                value = value['price']
            declared.append(value)
    allowed = {output['currency'] + '_PER_SHARE', output['currency'] + '_PER_UNIT',
               output['currency'] + '_PER_FUND_UNIT'}
    if any(not isinstance(x, str) or x not in allowed for x in declared) or len(set(declared)) > 1:
        raise Problem('price_unit_unsupported', 422)
    return frozenset(declared)


def _summary(output: dict) -> dict:
    """Display-ready subset; missing units never acquire invented semantics."""
    price_units = _check_units(output)
    def optional(name):
        if name not in output:
            return {'state': 'missing'}
        value = output[name]
        if value is None:
            return {'state': 'none'}
        if value.get('status') == 'unavailable':
            result = {'state': 'unavailable'}
            if isinstance(value.get('label'), str):
                result['label'] = value['label']
            return result
        result = {'state': 'provided', 'anchors': {key: value['anchors'][key] for key in ('L', 'M', 'H')}}
        if name == 'candidate':
            result['label'] = value['label']
        if name == 'option' and 'status' in value:
            result['status'] = value['status']
        if name == 'option' and 'value' in value:
            allowed = {output['currency'] + '_PER_SHARE', output['currency'] + '_PER_UNIT',
                       output['currency'] + '_PER_FUND_UNIT'}
            unit = value.get('value_unit')
            number_value = value['value']
            if not isinstance(unit, str) or unit not in allowed or unit not in price_units:
                result['additional_value'] = {'status': 'unit_unavailable'}
            elif isinstance(number_value, bool) or not isinstance(number_value, (int, float)):
                result['additional_value'] = {'status': 'invalid_data'}
            else:
                try:
                    finite = math.isfinite(number_value)
                except OverflowError:
                    finite = False
                result['additional_value'] = ({'status': 'provided', 'value': number_value, 'unit': unit}
                                              if finite else {'status': 'invalid_data'})
        return result

    parameter_names = {
        'discount_rate', 'terminal_discount_rate', 'transition_discount_rate',
        'growth_rate', 'terminal_growth_rate', 'perpetual_growth',
        'roe_forward', 'forward_roe', 'forward_return', 'steady_return',
    }
    def number(value):
        if value is None:
            return True
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        try:
            return math.isfinite(value)
        except OverflowError:
            return False

    def tiers(value):
        return (isinstance(value, dict) and bool(value) and set(value) <= {'L', 'M', 'H'}
                and all(number(item) for item in value.values()))

    def parameters(group):
        result = {}
        if not isinstance(group, dict):
            return result
        for key, value in group.items():
            if key not in parameter_names:
                continue
            if not isinstance(value, dict) or value.get('unit') != 'RATIO':
                result[key] = {'status': 'unit_unavailable'}
                continue
            data = {name: value[name] for name in ('values', 'selected_value', 'reference_value') if name in value}
            if (not data or any(not (tiers(v) if name == 'values' else number(v)) for name, v in data.items())):
                result[key] = {'status': 'invalid_data'}
                continue
            result[key] = {'status': 'provided', 'unit': 'RATIO', 'data': data}
        return result

    source = output.get('key_parameters')
    result = {'anchors': {key: output['anchors'][key] for key in ('L', 'M', 'H')},
              'candidate': optional('candidate'), 'option': optional('option'),
              'parameters': parameters(source), 'segments': {},
              'dates': {key: output.get(key) for key in (
                  'valuation_as_of', 'framework_as_of', 'historical_financial_basis_as_of', 'historical_financial_statement_as_of',
                  'historical_valuation_as_of', 'historical_research_as_of')},
              'review_flags': output.get('review_flags', []),
              'fund_reference_status': output.get('fund_reference_status')}
    conditional = output.get('conditional')
    if conditional is None or (isinstance(conditional, (str, list, dict)) and not conditional):
        result['conditional'] = {'state': 'missing'}
    elif isinstance(conditional, bool):
        result['conditional'] = {'state': 'provided', 'value': conditional, 'adopted': 'unknown'}
    elif isinstance(conditional, dict) and isinstance(conditional.get('is_trial'), bool):
        result['conditional'] = {'state': 'provided', 'is_trial': conditional['is_trial'], 'adopted': 'unknown'}
    else:
        result['conditional'] = {'state': 'provided', 'adopted': 'unknown'}
    segments = output.get('segments')
    if isinstance(segments, dict):
        for key, segment in segments.items():
            entry = {'parameters': parameters(source.get(key)) if isinstance(source, dict) else {}}
            if isinstance(segment, dict) and isinstance(segment.get('label'), str):
                entry['label'] = segment['label']
            allowed = {output['currency'] + '_PER_SHARE', output['currency'] + '_PER_UNIT',
                       output['currency'] + '_PER_FUND_UNIT'}
            if not isinstance(segment, dict) or not isinstance(segment.get('unit'), str) or segment['unit'] not in allowed:
                entry['status'] = 'unit_unavailable'
            elif not tiers(segment.get('values')):
                entry['status'] = 'invalid_data'
            else:
                entry.update({'status': 'provided', 'unit': segment['unit'], 'values': segment['values']})
            result['segments'][key] = entry
    return result


class ReviewService:
    """Store immutable review snapshots and device-bound explicit actions."""

    def __init__(self, path: Path, provider: Provider, devices: DeviceStore) -> None:
        self.path, self.provider, self.devices = Path(path), provider, devices
        self._read_only = False
        self._sandbox_verifier: ExecutionVerifier | None = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as con:
            con.executescript('''
                CREATE TABLE IF NOT EXISTS reviews (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, ticker TEXT NOT NULL,
                    generation TEXT NOT NULL, expected TEXT, document BLOB NOT NULL,
                    digest TEXT NOT NULL, csrf TEXT, state TEXT NOT NULL, execution_ref TEXT
                );
                CREATE TABLE IF NOT EXISTS results (
                    ticker TEXT PRIMARY KEY, version TEXT NOT NULL, review_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    version TEXT PRIMARY KEY, review_id TEXT UNIQUE NOT NULL,
                    owner TEXT NOT NULL, csrf TEXT NOT NULL, confirmed_at TEXT NOT NULL, snapshot_digest TEXT
                );
                CREATE TRIGGER IF NOT EXISTS immutable_review
                    BEFORE UPDATE OF owner,ticker,generation,expected,document,digest,execution_ref ON reviews
                    BEGIN SELECT RAISE(ABORT, 'immutable review'); END;
            ''')
            from lynch_reader.legacy_batches import create_tables
            create_tables(con)
            con.execute('BEGIN IMMEDIATE')
            columns = {row[1] for row in con.execute('PRAGMA table_info(events)')}
            if 'snapshot_digest' not in columns:
                # Old events are preserved but cannot be retrospectively bound
                # to a particular snapshot by inventing a digest.
                con.execute('ALTER TABLE events ADD COLUMN snapshot_digest TEXT')
            con.commit()

    @classmethod
    def open_readonly(cls, path: Path, provider: Provider) -> 'ReviewService':
        """Read existing recorded results without creating or changing the store."""
        service = cls.__new__(cls)
        service.path = Path(path)
        service.provider = provider
        service.devices = None
        service._sandbox_verifier = None
        service._read_only = True
        return service

    @contextmanager
    def _db(self):
        if self._read_only:
            con = sqlite3.connect(self.path.absolute().as_uri() + '?mode=ro', uri=True, timeout=10, isolation_level=None)
        else:
            con = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            con.execute('PRAGMA synchronous=FULL')
            yield con
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    @staticmethod
    def _version(con, ticker):
        row = con.execute('SELECT version FROM results WHERE ticker=?', (ticker,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def _row(con, rid, actor):
        return con.execute('SELECT id,owner,ticker,generation,expected,document,digest,csrf,state,execution_ref '
                           'FROM reviews WHERE id=? AND owner=?', (rid, actor)).fetchone()

    def _draft(self, row):
        document = bytes(row[5])
        if len(document) > MAX_SNAPSHOT_BYTES or not secrets.compare_digest(_hash(document), row[6]):
            raise Problem('invalid', 409)
        try:
            data = _load_json(document)
            _validate_draft(data)
            draft = Draft(document)
            output = draft.output
            if output['ticker'] != row[2] or output['currency'] != self.provider.currency(row[2]):
                raise Problem('invalid', 409)
            _check_units(output)
            if self.provider.generation(row[2]) != row[3] or not self.provider.current(row[2], draft):
                raise Problem('stale', 409)
            return draft
        except Problem:
            raise
        except (KeyError, ValueError, OSError, TypeError):
            raise Problem('stale_or_invalid', 409) from None

    @staticmethod
    def _event(con, rid, actor):
        return con.execute('SELECT version,csrf,snapshot_digest,confirmed_at FROM events WHERE review_id=? AND owner=?',
                           (rid, actor)).fetchone()

    @staticmethod
    def _event_snapshot_valid(row, event):
        """Check a recorded event's binding without consulting current sources.

        The event's authority is the trusted transactional store, not this
        ordinary digest. Unbound legacy events cannot be upgraded by guessing.
        """
        if (row[8] != 'confirmed' or not event or not isinstance(event[0], str) or not event[0]
                or not isinstance(event[1], str) or not re.fullmatch(r'[0-9a-f]{64}', event[1])
                or not isinstance(event[2], str) or not re.fullmatch(r'[0-9a-f]{64}', event[2])):
            return False
        try:
            document = bytes(row[5])
            if (len(document) > MAX_SNAPSHOT_BYTES or
                    not secrets.compare_digest(_hash(document), row[6]) or
                    not secrets.compare_digest(event[2], row[6])):
                return False
            data = _load_json(document)
            _validate_draft(data)
            confirmed_at = datetime.fromisoformat(event[3])
            if confirmed_at.tzinfo is None or confirmed_at.utcoffset() is None:
                return False
            if data['output']['ticker'] != row[2]:
                return False
            _check_units(data['output'])
            return True
        except (ValueError, TypeError, KeyError, Problem):
            return False

    def _status_snapshot(self, rid, actor):
        with self._db() as con:
            con.execute('BEGIN')
            row = self._row(con, rid, actor)
            if row is None:
                raise Problem('not_found', 404)
            current = self._version(con, row[2])
            event = self._event(con, rid, actor)
        return row, current, event

    def create(self, ticker: str, actor: str):
        try:
            generation = self.provider.generation(ticker)
            currency = self.provider.currency(ticker)
            if not isinstance(generation, str) or not generation:
                raise ValueError('invalid generation')
            with self._db() as con:
                expected = self._version(con, ticker)
            captured = self.provider.calculate(ticker)
            if not isinstance(captured, Captured):
                raise ValueError('invalid captured result')
            if captured.execution_ref is not None and not isinstance(captured.execution_ref, str):
                raise ValueError('invalid execution reference')
            draft = captured.draft
            if not isinstance(draft, Draft) or len(draft._document) > MAX_SNAPSHOT_BYTES:
                raise ValueError('invalid snapshot size')
            data = _load_json(draft._document)
            _validate_draft(data)
            if draft.output['ticker'] != ticker or draft.output['currency'] != currency:
                raise Problem('identity_mismatch', 400)
            _check_units(draft.output)
            if not self.provider.current(ticker, draft):
                raise Problem('stale', 409)
            rid = secrets.token_urlsafe(24)
            with self._db() as con:
                con.execute('BEGIN IMMEDIATE')
                if self._version(con, ticker) != expected or self.provider.generation(ticker) != generation:
                    raise Problem('version_or_spec_changed', 409)
                con.execute('INSERT INTO reviews VALUES (?,?,?,?,?,?,?,NULL,?,?)',
                            (rid, actor, ticker, generation, expected, draft._document, _hash(draft._document),
                             'pending', captured.execution_ref))
                con.commit()
            return {'review_id': rid}
        except Problem:
            raise
        except (KeyError, ValueError, OSError, TypeError, AttributeError, subprocess.SubprocessError):
            raise Problem('calculation_failed', 400) from None

    def view(self, rid: str, actor: str):
        with self._db() as con:
            con.execute('BEGIN IMMEDIATE')
            row = self._row(con, rid, actor)
            if row is None:
                raise Problem('not_found', 404)
            if row[8] != 'pending' or self._version(con, row[2]) != row[4]:
                raise Problem('version_conflict', 409)
            draft = self._draft(row)
            csrf = None
            try:
                _require_verified_execution(row[2], draft, row[9], self._sandbox_verifier)
            except ProofUnavailable:
                confirmation_status = 'proof_unavailable'
            else:
                confirmation_status = 'available'
                csrf = secrets.token_urlsafe(32)
                con.execute('UPDATE reviews SET csrf=? WHERE id=?', (_hash(csrf.encode()), rid))
                con.commit()
        # Full structured output and all declared snapshot material remain
        # available to the consumer; script-provided confirmation is not used.
        return {'review_id': rid, 'expected_version': row[4], 'output': draft.output,
                'summary': _summary(draft.output), 'snapshot': _load_json(draft._document)['inputs'], 'csrf': csrf,
                'confirmation_status': confirmation_status,
                'notice': '脚本输出不代表用户确认；完整依赖和执行来源尚未证明。'}

    def confirm(self, rid: str, token: str, csrf: str):
        actor = _hash(token.encode())
        # Lock order: device store, then result SQLite. No model calculation is
        # run while either lock is held. Source writers do not yet share a lock.
        try:
            with self.devices.lock_paired(token) as paired:
                if not paired:
                    raise Problem('unpaired', 401)
                with self._db() as con:
                    con.execute('BEGIN IMMEDIATE')
                    row = self._row(con, rid, actor)
                    if row is None:
                        raise Problem('not_found', 404)
                    if row[8] == 'confirmed':
                        # This only reports a past transaction; it never permits
                        # another confirmation or asserts current-source validity.
                        event = self._event(con, rid, actor)
                        if (not self._event_snapshot_valid(row, event) or
                                not secrets.compare_digest(event[1], _hash(csrf.encode()))):
                            raise Problem('invalid_event', 409)
                        code, version = 200, event[0]
                    else:
                        if self._version(con, row[2]) != row[4]:
                            raise Problem('version_conflict', 409)
                        draft = self._draft(row)
                        _require_verified_execution(row[2], draft, row[9], self._sandbox_verifier)
                        if row[7] is None or not secrets.compare_digest(row[7], _hash(csrf.encode())):
                            raise Problem('not_reviewed', 409)
                        version = secrets.token_urlsafe(24)
                        con.execute('INSERT INTO results VALUES (?,?,?) ON CONFLICT(ticker) DO UPDATE '
                                    'SET version=excluded.version, review_id=excluded.review_id', (row[2], version, rid))
                        con.execute("UPDATE reviews SET state='confirmed', csrf=NULL WHERE id=?", (rid,))
                        con.execute('INSERT INTO events (version,review_id,owner,csrf,confirmed_at,snapshot_digest) '
                                    'VALUES (?,?,?,?,?,?)',
                                    (version, rid, actor, _hash(csrf.encode()),
                                     datetime.now(timezone.utc).isoformat(), row[6]))
                        con.commit()
                        code = 201
                    ticker = row[2]
        except StoreCorrupt:
            raise Problem('unpaired', 401) from None
        except OSError:
            raise Problem('device_store_unavailable', 503) from None

        # This is an observation after commit, not a claim about future edits.
        observed = self.current(ticker)
        current_status = observed['status']
        if current_status == 'confirmed' and observed.get('review_id') != rid:
            current_status = 'superseded'
        return code, {'event_id': version, 'snapshot_confirmed': True,
                      'is_current': current_status == 'confirmed', 'current_status': current_status,
                      'checked_at': datetime.now(timezone.utc).isoformat()}

    def status(self, rid: str, actor: str):
        # Source verification is outside SQLite. If the review, event or
        # pointer changes, retry using a fresh consistent database snapshot.
        for _ in range(3):
            before = self._status_snapshot(rid, actor)
            row, current, event = before
            event_valid = self._event_snapshot_valid(row, event)
            if row[8] not in ('pending', 'confirmed'):
                state = 'invalid'
            elif row[8] == 'confirmed' and not event_valid:
                state = 'invalid'
            elif row[8] == 'pending' and event is not None:
                state = 'invalid'
            elif row[8] == 'pending' and current != row[4]:
                state = 'version_conflict'
            else:
                try:
                    draft = self._draft(row)
                    if event_valid and event[0] != current:
                        state = 'superseded'
                    else:
                        _require_verified_execution(row[2], draft, row[9], self._sandbox_verifier)
                        state = 'confirmed' if event_valid else 'ready'
                except Problem as exc:
                    state = exc.code
            if self._status_snapshot(rid, actor) != before:
                continue
            indeterminate = (row[8] not in ('pending', 'confirmed') or
                             (row[8] == 'confirmed' and not event_valid) or
                             (row[8] == 'pending' and event is not None))
            pointer_current = None if indeterminate else bool(event_valid and event[0] == current)
            return {'review_id': rid, 'event_id': event[0] if event_valid else None,
                    'recorded_event_id': event[0] if event and isinstance(event[0], str) else None,
                    'current_version': current, 'expected_version': row[4], 'result_status': state,
                    'snapshot_confirmed': None if indeterminate else event_valid,
                    'pointer_current': pointer_current, 'is_current': pointer_current is True and state == 'confirmed',
                    'checked_at': datetime.now(timezone.utc).isoformat()}
        return {'review_id': rid, 'event_id': None, 'recorded_event_id': None, 'current_version': None,
                'expected_version': None, 'result_status': 'changed', 'snapshot_confirmed': None,
                'pointer_current': None, 'is_current': False,
                'checked_at': datetime.now(timezone.utc).isoformat()}

    def current_for_display(self, ticker: str):
        """Read an intact recorded confirmation without treating it as fresh proof."""
        if not self._read_only:
            raise RuntimeError('snapshot display is only available to read-only consumers')
        with self._db() as con:
            con.execute('BEGIN')
            row = con.execute('SELECT r.id,r.owner,r.ticker,r.generation,r.expected,r.document,r.digest,r.csrf,r.state,r.execution_ref, '
                              'e.version,e.csrf,e.snapshot_digest,e.confirmed_at '
                              'FROM results c JOIN reviews r ON c.review_id=r.id '
                              'JOIN events e ON e.review_id=r.id AND e.version=c.version AND e.owner=r.owner '
                              'AND e.snapshot_digest=r.digest '
                              "WHERE c.ticker=? AND r.ticker=c.ticker AND r.state='confirmed'", (ticker,)).fetchone()
            version = self._version(con, ticker)
        if row is None:
            return self.current(ticker)
        if not self._event_snapshot_valid(row, row[10:14]):
            return {'status': 'invalid'}
        draft = Draft(bytes(row[5]))
        flags = ['confirmed_snapshot_unverified']
        try:
            inspect = getattr(self.provider, 'source_conditions', None)
            if callable(inspect):
                conditions = inspect(ticker, draft)
                if conditions is None:
                    return {'status': 'invalid'}
                flags.extend(conditions)
            if callable(getattr(self.provider, 'source_status', None)):
                from lynch_reader.legacy_batches import _source_observation
                _, status = _source_observation(self, ticker, draft, row[3])
            else:
                changed = (self.provider.generation(ticker) != row[3]
                           or self.provider.currency(ticker) != draft.output['currency']
                           or not self.provider.current(ticker, draft))
                status = 'changed' if changed else 'match'
            if status != 'match':
                flags.append('valuation_source_' + status)
        except Exception:  # noqa: BLE001 — the confirmed snapshot outlives its sources
            flags.append('valuation_source_unavailable')
        with self._db() as con:
            if self._version(con, ticker) != version:
                return {'status': 'changed'}
        return {'status': 'confirmed_snapshot', 'review_id': row[0], 'confirmed_at': row[13],
                'historical_evidence_flags': list(dict.fromkeys(flags)), 'output': draft.output}

    def current(self, ticker: str):
        with self._db() as con:
            con.execute('BEGIN')
            row = con.execute('SELECT r.id,r.owner,r.ticker,r.generation,r.expected,r.document,r.digest,r.csrf,r.state,r.execution_ref, '
                              'e.version,e.csrf,e.snapshot_digest,e.confirmed_at '
                              'FROM results c JOIN reviews r ON c.review_id=r.id '
                              'JOIN events e ON e.review_id=r.id AND e.version=c.version AND e.owner=r.owner '
                              'AND e.snapshot_digest=r.digest '
                              "WHERE c.ticker=? AND r.ticker=c.ticker AND r.state='confirmed'", (ticker,)).fetchone()
            exists = self._version(con, ticker)
        if row is None:
            if exists is None:
                return {'status': 'missing'}
            from lynch_reader.legacy_batches import current
            return current(self, ticker, exists)
        if not self._event_snapshot_valid(row, row[10:14]):
            return {'status': 'invalid'}
        try:
            draft = self._draft(row)
            _require_verified_execution(ticker, draft, row[9], self._sandbox_verifier)
        except ProofUnavailable:
            return {'status': 'proof_unavailable'}
        except Problem as exc:
            return {'status': exc.code}
        # Do not return an old value as current if another confirmation won
        # while we inspected the sources or execution record.
        with self._db() as con:
            if self._version(con, ticker) != exists:
                return {'status': 'changed'}
        return {'status': 'confirmed', 'review_id': row[0], 'confirmed_at': row[13], 'output': draft.output}

    def accept_legacy_batch(self, authorization, *, archive, snapshots, evidence):
        """Local-only operator attestation; no HTTP route is provided."""
        from lynch_reader.legacy_batches import accept
        return accept(self, authorization, archive=archive, snapshots=snapshots, evidence=evidence)


def review_routes(service: ReviewService, devices: DeviceStore) -> list[Route]:
    """Routes for explicit integration behind DeviceAuth; each also checks pairing."""
    async def body(request):
        if not is_json(request):
            raise Problem('json_required', 415)
        raw = await _read_at_most(request, 1024)
        if raw is None:
            raise Problem('body_too_large', 413)
        try:
            value = _load_json(raw)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, UnicodeError):
            raise Problem('invalid_body', 400) from None

    def token(request):
        value = request.cookies.get(COOKIE, '')
        try:
            if not value or not devices.is_paired(value):
                raise Problem('unpaired', 401)
        except StoreCorrupt:
            raise Problem('unpaired', 401) from None
        return value

    async def guarded(request, operation):
        try:
            credential = token(request)
            return await operation(request, credential)
        except Problem as exc:
            return JSONResponse({'error': exc.code}, status_code=exc.status, headers={'Cache-Control': 'no-store'})
        except sqlite3.Error:
            return JSONResponse({'error': 'storage_failure'}, status_code=503)

    async def create_op(request, credential):
        data = await body(request)
        if set(data) != {'ticker'} or not isinstance(data['ticker'], str):
            raise Problem('invalid_body', 400)
        result = await run_in_threadpool(service.create, data['ticker'], _hash(credential.encode()))
        return JSONResponse(result, status_code=201, headers={'Cache-Control': 'no-store'})

    async def view_op(request, credential):
        result = await run_in_threadpool(service.view, request.path_params['rid'], _hash(credential.encode()))
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})

    async def confirm_op(request, credential):
        if request.headers.get('origin') != str(request.base_url).rstrip('/'):
            raise Problem('origin_required', 403)
        data = await body(request)
        if set(data) != {'csrf'} or not isinstance(data['csrf'], str):
            raise Problem('invalid_body', 400)
        code, result = await run_in_threadpool(service.confirm, request.path_params['rid'], credential, data['csrf'])
        return JSONResponse(result, status_code=code, headers={'Cache-Control': 'no-store'})

    async def status_op(request, credential):
        result = await run_in_threadpool(service.status, request.path_params['rid'], _hash(credential.encode()))
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})

    async def current_op(request, credential):
        result = await run_in_threadpool(service.current, request.path_params['ticker'])
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})

    async def create(request):
        return await guarded(request, create_op)
    async def view(request):
        return await guarded(request, view_op)
    async def confirm(request):
        return await guarded(request, confirm_op)
    async def status(request):
        return await guarded(request, status_op)
    async def current(request):
        return await guarded(request, current_op)

    return [Route('/api/valuation/reviews', create, methods=['POST']),
            Route('/api/valuation/reviews/{rid}', view, methods=['GET']),
            Route('/api/valuation/reviews/{rid}/confirm', confirm, methods=['POST']),
            Route('/api/valuation/reviews/{rid}/status', status, methods=['GET']),
            Route('/api/valuation/current/{ticker}', current, methods=['GET'])]
