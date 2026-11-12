"""Offline calculation drafts; this module cannot authorize or publish valuations.

The caller must supply the complete input set and trusted calculator. Python is
not sandboxed: calculators can access external files or the network. Drafts and
hashes are evidence to review, not evidence of user confirmation.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import uuid
from datetime import date
from dataclasses import dataclass

MAX_RECORD_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Draft:
    # Canonical bytes keep the reviewed inputs and output unchanged in memory.
    _document: bytes
    revision: str | None = None

    @property
    def output(self):
        return _load_json(self._document)['output']


class InputChanged(ValueError):
    pass


class InvalidDraft(ValueError):
    pass


class RevisionConflict(ValueError):
    pass


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _load_json(data):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise InvalidDraft('duplicate JSON key')
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=unique_object)


def _digest(value):
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _inputs(root, names):
    root = Path(root).resolve(strict=True)
    files = {}
    for name in names:
        p = PurePosixPath(name)
        if not name or p.is_absolute() or any(x in ('', '.', '..') for x in name.split('/')):
            raise ValueError('input must be a relative path')
        path = root
        for part in p.parts:
            path = path / part
            if path.is_symlink():
                raise ValueError('symlink input is not supported')
        if name in files:
            raise ValueError('duplicate input')
        files[name] = base64.b64encode(path.read_bytes()).decode('ascii')
    if not files:
        raise ValueError('empty input set')
    return files


def _validate_output(output):
    if not isinstance(output, dict):
        raise InvalidDraft('output must be an object')
    ticker, currency = output.get('ticker'), output.get('currency')
    if not isinstance(ticker, str) or not re.fullmatch(r'[A-Z0-9]+\.[A-Z]+', ticker):
        raise InvalidDraft('invalid ticker')
    if not isinstance(currency, str) or not re.fullmatch(r'[A-Z]{3}', currency):
        raise InvalidDraft('invalid currency')
    for key in ('valuation_as_of', 'framework_as_of', 'historical_financial_basis_as_of', 'historical_financial_statement_as_of',
                'historical_valuation_as_of', 'historical_research_as_of'):
        try:
            value = output[key] if key in ('valuation_as_of', 'framework_as_of') else output.get(key)
            if value is not None and (not isinstance(value, str) or date.fromisoformat(value).isoformat() != value):
                raise ValueError
        except (KeyError, ValueError):
            raise InvalidDraft('invalid date') from None
    def band(value):
        if (not isinstance(value, dict) or not {'L', 'M', 'H'} <= set(value)
                or set(value) - {'L', 'M', 'H', 'unit', 'units'}):
            raise InvalidDraft('invalid band')
        numbers = [value[k] for k in ('L', 'M', 'H')]
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x < 0 for x in numbers):
            raise InvalidDraft('invalid band values')
        if not numbers[0] < numbers[1] < numbers[2]:
            raise InvalidDraft('band is not increasing')
    band(output.get('anchors'))
    candidate = output.get('candidate')
    if candidate is not None:
        if not isinstance(candidate, dict):
            raise InvalidDraft('invalid candidate')
        if candidate.get('status') is not None and (not isinstance(candidate['status'], str) or not candidate['status'].strip()):
            raise InvalidDraft('invalid candidate status')
        if candidate.get('status') == 'unavailable':
            if candidate.get('anchors') is not None:
                raise InvalidDraft('unavailable candidate cannot contain anchors')
            if candidate.get('label') is not None and (not isinstance(candidate['label'], str) or not candidate['label'].strip()):
                raise InvalidDraft('invalid candidate label')
        else:
            if not isinstance(candidate.get('label'), str) or not candidate['label'].strip():
                raise InvalidDraft('invalid candidate')
            band(candidate.get('anchors'))
    option = output.get('option')
    if option is not None:
        if not isinstance(option, dict) or 'adjusted_anchors' in option:
            raise InvalidDraft('invalid or ambiguous option')
        if 'status' in option and (not isinstance(option['status'], str) or not option['status'].strip()):
            raise InvalidDraft('invalid option status')
        if option.get('status') == 'unavailable':
            if option.get('anchors') is not None:
                raise InvalidDraft('unavailable option cannot contain anchors')
        else:
            band(option.get('anchors'))
    flags = output.get('review_flags', [])
    if not isinstance(flags, list) or any(not isinstance(flag, str) or not flag.strip() for flag in flags):
        raise InvalidDraft('invalid review flags')
    fund_status = output.get('fund_reference_status')
    if fund_status is not None and (not isinstance(fund_status, str) or not fund_status.strip()):
        raise InvalidDraft('invalid fund reference status')


def _validate_draft(draft):
    if not isinstance(draft, dict) or set(draft) != {'format', 'entry', 'inputs', 'runtime', 'output'} or draft['format'] != 1:
        raise InvalidDraft('invalid draft structure')
    if not isinstance(draft['inputs'], dict) or draft['entry'] not in draft['inputs']:
        raise InvalidDraft('invalid input snapshot')
    for value in draft['inputs'].values():
        try:
            base64.b64decode(value, validate=True)
        except (ValueError, TypeError):
            raise InvalidDraft('invalid input bytes') from None
    _validate_output(draft['output'])
    try:
        _json_bytes(draft)
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise InvalidDraft('invalid JSON data') from None


# This is an observation and guard for trusted Python calculators, not an OS
# sandbox: native code, pre-opened descriptors and non-open operations can
# evade Python audit events. Runtime files remain host dependencies.
_AUDIT_RUNNER = r"""
import importlib.util, json, os, runpy, sys
fd = int(sys.argv[1])
entry = sys.argv[2]
allowed = set(json.loads(sys.argv[3]))
arguments = json.loads(sys.argv[4])
stdlib = sys.argv[5]

def audit(event, args):
    if event != 'open' or not isinstance(args[0], (str, bytes, os.PathLike)):
        return
    raw_path = os.fsdecode(args[0])
    if not os.path.isabs(raw_path):
        item = {'path': raw_path, 'allowed': False, 'runtime': False, 'probe': False}
        os.write(fd, (json.dumps(item) + '\n').encode())
        raise PermissionError('relative file opens cannot be attributed')
    path = os.path.realpath(raw_path)
    flags = args[2]
    writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
    runtime = path.startswith(stdlib + os.sep) and '/site-packages/' not in path and '/dist-packages/' not in path
    probe = False
    if not writing and path.endswith('.pyc') and not os.path.exists(path):
        try:
            probe = importlib.util.source_from_cache(path) in allowed
        except ValueError:
            pass
    accepted = not writing and (path in allowed or runtime or probe)
    item = {'path': path, 'allowed': accepted, 'runtime': runtime, 'probe': probe}
    os.write(fd, (json.dumps(item) + '\n').encode())
    if not accepted:
        raise PermissionError('undeclared file access')

sys.argv = [entry] + arguments
sys.path.insert(0, os.getcwd())
sys.addaudithook(audit)
runpy.run_path(entry, run_name='__main__')
"""


def calculate(root, inputs, entry, *, arguments=()):
    """Calculate using copied inputs and return an unconfirmed review draft.

    The input list is caller-supplied, not an automatically verified dependency
    closure. External state and the Python runtime are not isolated or frozen.
    """
    names = list(inputs)
    arguments = list(arguments)
    if any(not isinstance(arg, str) for arg in arguments):
        raise ValueError('arguments must be strings')
    snapshot = _inputs(root, names)
    if entry not in snapshot:
        raise ValueError('entry must be included in inputs')
    temp_root = Path.cwd() / '_local' / 'tmp'
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='valuation-snapshot-', dir=temp_root) as tmp:
        target = Path(tmp)
        for name, data in snapshot.items():
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(data))
        with tempfile.TemporaryFile(dir=temp_root) as log:
            allowed = [str(target / name) for name in names]
            stdlib = str(Path(os.__file__).resolve().parent)
            cmd = [sys.executable, '-I', '-S', '-B', '-c', _AUDIT_RUNNER,
                   str(log.fileno()), str(target / entry), json.dumps(allowed), json.dumps(arguments), stdlib]
            proc = subprocess.run(cmd, cwd=target, env={'PYTHONIOENCODING': 'utf-8'},
                                  stdin=subprocess.DEVNULL, capture_output=True, timeout=60,
                                  pass_fds=(log.fileno(),))
            log.seek(0)
            events = [_load_json(line) for line in log]
        denied = sorted({item['path'] for item in events if not item['allowed']})
        if denied:
            raise InputChanged(f'calculator attempted undeclared file access: {denied}')
        proc.check_returncode()
        read_audit = {'enabled': True,
                      'snapshot_path_attempts': sorted({str(Path(item['path']).relative_to(target))
                                                for item in events if not item['runtime'] and not item['probe']}),
                      'runtime_path_attempts': sorted({item['path'] for item in events if item['runtime']}),
                      'runner_sha256': hashlib.sha256(_AUDIT_RUNNER.encode()).hexdigest()}
        if _inputs(target, names) != snapshot:
            raise InputChanged('calculator changed snapshot')
    if _inputs(root, names) != snapshot:
        raise InputChanged('source inputs changed during calculation')
    try:
        output = _load_json(proc.stdout)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise InvalidDraft('calculator output is not JSON') from exc
    draft = {'format': 1, 'entry': entry, 'inputs': snapshot,
             'runtime': {'python': sys.version, 'arguments': arguments, 'read_audit': read_audit}, 'output': output}
    _validate_draft(draft)
    return Draft(_json_bytes(draft))


def freshness(draft, root):
    """Compare declared inputs; this does not assess missing or external data."""
    document = _load_json(draft._document)
    _validate_draft(document)
    try:
        return 'inputs_match' if _inputs(root, document['inputs']) == document['inputs'] else 'stale'
    except (OSError, ValueError):
        return 'stale'


def _read_record(path):
    try:
        with path.open('rb') as stream:
            raw = stream.read(MAX_RECORD_BYTES + 1)
        if len(raw) > MAX_RECORD_BYTES:
            raise InvalidDraft('draft too large')
        record = _load_json(raw)
        if not isinstance(record, dict) or set(record) != {'revision', 'digest', 'draft'}:
            raise InvalidDraft('invalid record')
        if not isinstance(record['revision'], str) or not re.fullmatch(r'[0-9a-f]{32}', record['revision']):
            raise InvalidDraft('invalid revision')
        _validate_draft(record['draft'])
        if _digest(record['draft']) != record['digest']:
            raise InvalidDraft('draft integrity mismatch')
        return record
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError, OverflowError) as exc:
        if isinstance(exc, InvalidDraft):
            raise
        raise InvalidDraft('invalid record') from exc


def load(path, *, ticker, currency):
    """Load a draft for an explicitly identified security and currency."""
    record = _read_record(Path(path))
    output = record['draft']['output']
    if output['ticker'] != ticker or output['currency'] != currency:
        raise InvalidDraft('identity mismatch')
    return Draft(_json_bytes(record['draft']), record['revision'])


def save(path, draft, *, expected_revision):
    """Atomically save a draft only if its observed revision has not changed.

    All writers must use this lock. A failure after replace may leave the new
    revision on disk; callers must re-read rather than assume it failed.
    """
    document = _load_json(draft._document)
    _validate_draft(document)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + '.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing = _read_record(path) if path.exists() else None
        current = existing['revision'] if existing is not None else None
        if current != expected_revision:
            raise RevisionConflict('draft revision changed')
        if existing is not None:
            prior = existing['draft']['output']
            output = document['output']
            if (prior['ticker'], prior['currency']) != (output['ticker'], output['currency']):
                raise InvalidDraft('identity mismatch')
        revision = uuid.uuid4().hex
        record = {'revision': revision, 'digest': _digest(document), 'draft': document}
        payload = _json_bytes(record)
        if len(payload) > MAX_RECORD_BYTES:
            raise InvalidDraft('draft too large')
        fd, temp = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as out:
                out.write(payload)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp, path)
            dirfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        return revision
