"""Restricted, offline declaration runner pilot; not a completeness proof.

Only trusted server configuration chooses roots and ticker. The sandbox is
read-only and network-isolated, but the current runtime/dependency closure and
cross-file generation are not independently established; confirmation stays off.
"""
from __future__ import annotations

import ast
import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
from typing import Mapping

from eval.lib import external_history, valuation_runner
from eval.lib.external_history import FORBIDDEN as _FORBIDDEN, MAX_SOURCE as _MAX_SOURCE
from eval.lib.external_history import open_regular as _open_regular, read_history, validate_history_path
from eval.lib.valuation_drafts import Draft, _json_bytes, _load_json, _validate_draft
from lynch_reader.auth.store import DeviceStore
from lynch_reader.valuation_reviews import Captured, ProofUnavailable, ReviewService

_TICKER = re.compile(r'^[A-Z0-9]{1,10}\.(?:US|HK|SH|SZ|SG)$')
_NAME = re.compile(r'^[A-Za-z][A-Za-z0-9_]*$')
_MAX_RESULT = 16 * 1024 * 1024


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha(path: Path) -> str:
    with path.open('rb') as stream:
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class RuntimeSnapshot:
    root: Path
    files: tuple[tuple[str, str], ...]
    python_version: str

    def check(self) -> None:
        for relative, expected in self.files:
            if _safe_file_sha(self.root, relative) != expected:
                raise ValueError('runtime snapshot changed')
        # Extra files would also be readable inside the sandbox.
        actual = {str(p.relative_to(self.root)) for p in self.root.rglob('*') if p.is_symlink() or p.is_file()}
        if actual != {name for name, _ in self.files}:
            raise ValueError('runtime snapshot file set changed')


def prepare_runtime(conda_root: Path, destination: Path) -> RuntimeSnapshot:
    """Copy a limited conda stdlib/runtime; excludes site packages and config.

    This is an offline compatibility pilot, not proof of runtime closure.
    Unsupported extensions, third-party packages and host kernel remain outside
    its assurance. The destination must be new and is not automatically removed.
    """
    if platform.machine() != 'x86_64':
        raise ValueError('runtime pilot currently requires x86_64')
    conda_root = Path(conda_root).resolve(strict=True)
    source_python = conda_root / 'bin/python3.12'
    source_stdlib = conda_root / 'lib/python3.12'
    if source_python.is_symlink() or not source_python.is_file() or not source_stdlib.is_dir():
        raise ValueError('expected conda Python 3.12 is unavailable')
    destination = Path(destination).absolute()
    destination.mkdir(parents=True, exist_ok=False)
    (destination / 'bin').mkdir()
    (destination / 'lib').mkdir()
    shutil.copy2(source_python, destination / 'bin/python3.12')
    stdlib = destination / 'lib/python3.12'
    excluded_dirs = {'site-packages', '__pycache__', 'test', 'tests', 'venv', 'ensurepip'}
    for folder, directories, names in os.walk(source_stdlib, followlinks=False):
        directories[:] = [d for d in directories if d not in excluded_dirs and not _FORBIDDEN.search(d)
                          and not (Path(folder) / d).is_symlink()]
        for name in names:
            source = Path(folder) / name
            if source.is_symlink() or _FORBIDDEN.search(name) or not (name.endswith('.py') or name.endswith('.so')):
                continue
            target = stdlib / source.relative_to(source_stdlib)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    libraries = {}
    for binary in [destination / 'bin/python3.12', *stdlib.rglob('*.so')]:
        result = subprocess.run(['/usr/bin/ldd', str(binary)], capture_output=True, text=True, timeout=10,
                                env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
        for line in result.stdout.splitlines():
            match = re.match(r'\s*(\S+)\s+=>\s+(/\S+)', line)
            if not match:
                continue
            name, source = match.group(1), Path(match.group(2)).resolve(strict=True)
            if '/' in name or _FORBIDDEN.search(name):
                raise ValueError('unsafe runtime library name')
            trusted_roots = (conda_root / 'lib', Path('/usr/lib'), Path('/lib'))
            if not any(source.is_relative_to(root.resolve()) for root in trusted_roots):
                raise ValueError('runtime library outside trusted roots')
            if name in libraries and libraries[name] != source:
                raise ValueError('ambiguous runtime library')
            libraries[name] = source
    for name, source in libraries.items():
        shutil.copy2(source, destination / 'lib' / name)
    loader = Path('/lib64/ld-linux-x86-64.so.2').resolve(strict=True)
    shutil.copy2(loader, destination / 'loader')
    files = tuple(sorted((str(p.relative_to(destination)), _file_sha(p))
                         for p in destination.rglob('*') if p.is_file()))
    return RuntimeSnapshot(destination.resolve(), files, 'conda-python3.12 (patch version unverified)')


def _safe_read(root: Path, relative: str) -> bytes:
    with os.fdopen(_open_regular(root, relative), 'rb') as stream:
        data = stream.read(_MAX_SOURCE + 1)
    if len(data) > _MAX_SOURCE:
        raise ValueError('declared input too large')
    return data


def _safe_file_sha(root: Path, relative: str) -> str:
    digest = hashlib.sha256()
    with os.fdopen(_open_regular(root, relative), 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _declaration(source: bytes, *, required: bool) -> tuple[list[str], dict[str, str]]:
    tree = ast.parse(source)
    values = [item.value for item in tree.body if isinstance(item, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id == 'DEPENDENCIES' for t in item.targets)]
    if not values and not required:
        return [], {}
    if len(values) != 1:
        raise ValueError('DEPENDENCIES must appear exactly once')
    try:
        libs, axes, _ = valuation_runner._declaration(source)
        if any(_FORBIDDEN.search(name) for name in list(libs) + list(axes) + list(axes.values())):
            raise ValueError('unsafe dependency name')
        return list(libs), dict(axes)
    except (TypeError, SyntaxError) as exc:
        raise ValueError('invalid dependency declaration') from exc


@dataclass(frozen=True)
class SandboxConfig:
    code_root: Path
    content_root: Path
    runtime: RuntimeSnapshot
    work_root: Path
    registry_path: Path
    tickers: Mapping[str, str]
    rate_inputs: Mapping | None = None
    # Each configured security explicitly names its own local history file.
    # This is server configuration, never an HTTP-supplied path.
    external_sources: Mapping[str, Path] | None = None


class SandboxProvider:
    """Server-configured provider; HTTP can choose only a catalog ticker."""
    def __init__(self, config: SandboxConfig, *, read_only: bool = False) -> None:
        self.config = config
        self.code_selected = Path(config.code_root).absolute()
        self.content_selected = Path(config.content_root).absolute()
        self.code = self.code_selected.resolve(strict=True)
        self.server = Path(__file__).resolve().parent
        self.content = self.content_selected.resolve(strict=True)
        self.work = None
        self.registry = None
        if not read_only:
            work = Path(config.work_root).resolve()
            allowed = (Path.cwd() / '_local/tmp').resolve()
            if not work.is_relative_to(allowed):
                raise ValueError('temporary work must be under current directory _local/tmp')
            work.mkdir(parents=True, exist_ok=True)
            self.work = work
            self.registry = Path(config.registry_path).absolute()
            self.registry.parent.mkdir(parents=True, exist_ok=True)
        # Freeze trusted configuration; later caller mutation cannot alter it.
        self.tickers = dict(config.tickers)
        self.parameters = _load_json(_json_bytes({'rate_inputs': config.rate_inputs}))
        self.external_sources = {}
        for ticker, source in (config.external_sources or {}).items():
            if ticker not in self.tickers:
                raise ValueError('external source ticker is not configured')
            source = Path(source)
            validate_history_path(source)
            # The history may later be moved or temporarily unavailable; that
            # must not prevent a service restart or erase its frozen result.
            parent = source.parent.resolve(strict=False)
            self.external_sources[ticker] = (source, parent)
        if self.registry is not None:
            with sqlite3.connect(self.registry) as con:
                con.execute('CREATE TABLE IF NOT EXISTS executions (id TEXT PRIMARY KEY, ticker TEXT NOT NULL, '
                            'document BLOB NOT NULL, generation TEXT NOT NULL, runtime TEXT NOT NULL)')

    def _bwrap_path(self):
        path = Path('/usr/bin/bwrap')
        return str(path) if path.is_file() else None

    def _bwrap_generation(self):
        path = self._bwrap_path()
        if not path:
            raise ValueError('bwrap unavailable')
        return _file_sha(Path(path))

    def currency(self, ticker: str) -> str:
        if not isinstance(ticker, str) or not _TICKER.fullmatch(ticker):
            raise ValueError('invalid ticker')
        return self.tickers[ticker]

    def _check_roots(self):
        if (self.code_selected.resolve(strict=True) != self.code or
                self.content_selected.resolve(strict=True) != self.content):
            raise ValueError('configured source root changed')

    def _roots(self, ticker=None):
        roots = {'code': str(self.code), 'content': str(self.content), 'server': str(self.server),
                 'code_selected': str(self.code_selected), 'content_selected': str(self.content_selected)}
        sources = {name: str(source) for name, (source, _) in self.external_sources.items()
                   if ticker is None or name == ticker}
        if sources:
            roots['external_sources'] = sources
        return roots

    @staticmethod
    def _scoped_roots(roots, ticker):
        # Old captures may contain other tickers' paths. Preserve those bytes
        # for integrity checks, but exclude them from this ticker's identity.
        scoped = dict(roots)
        source = scoped.pop('external_sources', {}).get(ticker)
        if source is not None:
            scoped['external_sources'] = {ticker: source}
        return scoped

    def _external_bytes(self, ticker: str) -> bytes | None:
        selected = self.external_sources.get(ticker)
        if selected is None:
            return None
        source, parent = selected
        return read_history(source, parent)

    def _snapshot(self, ticker: str, *, include_external: bool = True) -> dict[str, bytes]:
        self.currency(ticker)
        self._check_roots()
        files = {'server/sandbox_valuation.py': _safe_read(self.server, 'sandbox_valuation.py'),
                 'server/valuation_reviews.py': _safe_read(self.server, 'valuation_reviews.py'),
                 'code/eval/lib/external_history.py': _safe_read(self.code, 'eval/lib/external_history.py'),
                 'code/eval/lib/valuation_runner.py': _safe_read(self.code, 'eval/lib/valuation_runner.py'),
                 'code/eval/lib/__init__.py': _safe_read(self.code, 'eval/lib/__init__.py'),
                 f'content/eval/{ticker}.py': _safe_read(self.content, f'eval/{ticker}.py')}
        if files['code/eval/lib/valuation_runner.py'] != Path(valuation_runner.__file__).read_bytes():
            raise ValueError('configured runner differs from server declaration parser')
        if files['code/eval/lib/external_history.py'] != Path(external_history.__file__).read_bytes():
            raise ValueError('configured history reader differs from loaded source')
        if include_external and ticker in self.external_sources:
            files[f'external/{ticker}.json'] = self._external_bytes(ticker)
        pending_libs, pending_axes = _declaration(files[f'content/eval/{ticker}.py'], required=True)
        axes = {}
        def enqueue_axes(more):
            for alias, module in more.items():
                if ((alias in axes and axes[alias] != module) or
                        (alias in pending_axes and pending_axes[alias] != module)):
                    raise ValueError('conflicting axis alias')
                pending_axes[alias] = module
        while pending_libs or pending_axes:
            while pending_libs:
                name = pending_libs.pop()
                key = f'code/eval/lib/{name}.py'
                if key in files:
                    continue
                files[key] = _safe_read(self.code, f'eval/lib/{name}.py')
                libs, child_axes = _declaration(files[key], required=False)
                pending_libs.extend(libs)
                enqueue_axes(child_axes)
            if pending_axes:
                alias, name = pending_axes.popitem()
                if alias in axes and axes[alias] != name:
                    raise ValueError('conflicting axis alias')
                axes[alias] = name
                key = f'content/axes/{name}.py'
                if key in files:
                    continue
                files[key] = _safe_read(self.content, f'axes/{name}.py')
                libs, child_axes = _declaration(files[key], required=False)
                pending_libs.extend(libs)
                enqueue_axes(child_axes)
        self._check_roots()
        return files

    def _runtime_generation(self) -> str:
        self.config.runtime.check()
        return _sha(_json_bytes({'files': self.config.runtime.files,
                                 'python_version': self.config.runtime.python_version}))

    def _generation_for_files(self, ticker: str, files: dict[str, bytes]) -> str:
        return _sha(_json_bytes({'ticker': ticker, 'currency': self.currency(ticker),
                                 'roots': self._roots(ticker),
                                 'files': {name: _sha(data) for name, data in files.items()},
                                 'parameters': self.parameters, 'runtime': self._runtime_generation(),
                                 'bwrap': self._bwrap_generation()}))

    def generation(self, ticker: str) -> str:
        return self._generation_for_files(ticker, self._snapshot(ticker))

    def frozen_generation(self, ticker: str, draft: Draft, *, scoped: bool = False) -> str:
        """Identify the immutable captured inputs, without claiming they are current."""
        document = _load_json(draft._document)
        _validate_draft(document)
        if document['output']['ticker'] != ticker:
            raise ValueError('frozen identity mismatch')
        files = {name: base64.b64decode(value, validate=True) for name, value in document['inputs'].items()}
        runtime = document['runtime']
        roots = self._scoped_roots(runtime['roots'], ticker) if scoped else runtime['roots']
        return _sha(_json_bytes({'ticker': ticker, 'currency': document['output']['currency'],
                                 'roots': roots,
                                 'files': {name: _sha(data) for name, data in files.items()},
                                 'parameters': runtime['parameters'], 'runtime': runtime['runtime_fingerprint'],
                                 'bwrap': runtime['bwrap_sha256']}))

    def generation_matches(self, ticker: str, draft: Draft) -> bool:
        return self.generation(ticker) == self.frozen_generation(ticker, draft, scoped=True)

    def _ensure_unchanged(self, ticker: str, files: dict[str, bytes], generation: str, runtime: str) -> None:
        if self._snapshot(ticker) != files or self.generation(ticker) != generation or self._runtime_generation() != runtime:
            raise ValueError('sandbox sources or runtime changed during calculation')

    def _make_draft(self, ticker: str, files: dict[str, bytes], runtime: str,
                    observed_python: str, output: dict) -> Draft:
        if not isinstance(output, dict) or output.get('ticker') != ticker or output.get('currency') != self.currency(ticker):
            raise ValueError('sandbox output identity mismatch')
        name = f'external/{ticker}.json'
        external = _load_json(files[name]) if name in files else None
        expected = valuation_runner._external_digest(external)
        if output.get('external_inputs_digest') != expected:
            raise ValueError('external input digest differs from frozen data')
        document = {'format': 1, 'entry': f'content/eval/{ticker}.py',
                    'inputs': {key: base64.b64encode(data).decode('ascii') for key, data in files.items()},
                    'runtime': {'python': self.config.runtime.python_version, 'observed_python': observed_python,
                                'runtime_fingerprint': runtime, 'parameters': self.parameters,
                                'roots': self._roots(ticker), 'bwrap_sha256': self._bwrap_generation(),
                                'sandbox': 'bwrap-unshare-all-read-only', 'closure': 'unproven'},
                    'output': output}
        _validate_draft(document)
        return Draft(_json_bytes(document))

    def calculate(self, ticker: str) -> Captured:
        if self.work is None or self.registry is None:
            raise ValueError('read-only source observer cannot calculate')
        bwrap = self._bwrap_path()
        if not bwrap:
            raise ValueError('bwrap unavailable')
        files = self._snapshot(ticker)
        generation = self.generation(ticker)
        runtime = self._runtime_generation()
        with tempfile.TemporaryDirectory(prefix='sandbox-valuation-', dir=self.work) as temp:
            snapshot = Path(temp) / 'snapshot'
            for name, data in files.items():
                path = snapshot / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            bootstrap = ("import json,sys; from pathlib import Path; sys.path.insert(0,'/work/code'); "
                         "from eval.lib.valuation_runner import run; "
                         "result=run(Path('/work/code'),Path('/work/content'),sys.argv[1],"
                         "rate_inputs=json.loads(sys.argv[2]), "
                         "external_inputs=json.loads(Path(sys.argv[3]).read_bytes()) if sys.argv[3] else None); "
                         "print(json.dumps({'python_version':sys.version,'result':result},ensure_ascii=False,allow_nan=False))")
            cmd = [bwrap, '--unshare-all', '--cap-drop', 'ALL', '--die-with-parent', '--clearenv', '--dir', '/lib64',
                   '--ro-bind', str(self.config.runtime.root / 'loader'), '/lib64/ld-linux-x86-64.so.2',
                   '--ro-bind', str(self.config.runtime.root), '/runtime', '--ro-bind', str(snapshot), '/work',
                   '--setenv', 'LD_LIBRARY_PATH', '/runtime/lib', '--chdir', '/work', '--dev', '/dev', '--proc', '/proc',
                   '--', '/runtime/bin/python3.12', '-I', '-S', '-B', '-c', bootstrap,
                   ticker, json.dumps(self.parameters['rate_inputs'], ensure_ascii=False, allow_nan=False),
                   f'/work/external/{ticker}.json' if f'external/{ticker}.json' in files else '']
            with tempfile.TemporaryFile(dir=self.work) as out, tempfile.TemporaryFile(dir=self.work) as err:
                try:
                    proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=err, timeout=60, env={})
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise ValueError('sandbox execution unavailable') from exc
                if proc.returncode:
                    raise ValueError('sandbox calculation failed')
                out.seek(0, os.SEEK_END)
                if out.tell() > _MAX_RESULT:
                    raise ValueError('sandbox output too large')
                out.seek(0)
                envelope = _load_json(out.read())
                if not isinstance(envelope, dict) or not isinstance(envelope.get('python_version'), str):
                    raise ValueError('invalid sandbox runtime report')
                output = envelope.get('result')
        self._ensure_unchanged(ticker, files, generation, runtime)
        ref = secrets.token_urlsafe(32)
        draft = self._make_draft(ticker, files, runtime, envelope['python_version'], output)
        with sqlite3.connect(self.registry) as con:
            con.execute('INSERT INTO executions VALUES (?,?,?,?,?)', (ref, ticker, draft._document, generation, runtime))
        return Captured(draft, ref)

    def _runner_result_conditions(self, ticker, document):
        """Observe the declared runner inputs without claiming full execution proof."""
        output = document['output']
        valuation_runner._validate_response(output, ticker)
        identity = output['root_identity']
        saved_roots = {name: Path(identity[f'{name}_resolved']) for name in ('code', 'content')}
        flags = ['valuation_source_unavailable', 'valuation_runtime_unavailable']
        if saved_roots != {'code': self.code, 'content': self.content}:
            flags.append('valuation_source_changed')
        try:
            if output['currency'] != self.currency(ticker):
                flags.append('valuation_source_changed')
        except (KeyError, ValueError):
            flags.append('valuation_source_unavailable')
        consumed = set()
        for key, record in output['source_paths'].items():
            root = 'content' if key == 'stock' or key.startswith('axis:') else 'code'
            selected, resolved = Path(record['selected']), Path(record['resolved'])
            relative = selected.relative_to(saved_roots[root])
            name = root + '/' + relative.as_posix()
            if name in consumed:
                return None
            consumed.add(name)
            frozen = base64.b64decode(document['inputs'][name], validate=True)
            if _sha(frozen) != output['source_versions'][key]:
                return None
            current = self.content if root == 'content' else self.code
            try:
                path = current / relative
                if path.resolve(strict=True) != resolved or _safe_read(current, relative.as_posix()) != frozen:
                    flags.append('valuation_source_changed')
            except (OSError, ValueError):
                flags.append('valuation_source_unavailable')
        peers = output.get('peer_snapshots', {})
        if peers or output.get('peer_missing'):
            # The frozen peer sources can be checked, but the currently
            # published peer result is not bound by this read-only observer.
            flags.append('peer_result_unverified')
        for peer in peers.values():
            valuation_runner._validate_response(peer, peer['ticker'])
            peer_identity = peer['root_identity']
            if any(Path(peer_identity[f'{name}_resolved']) != saved_roots[name] for name in ('code', 'content')):
                return None
            for key, record in peer['source_paths'].items():
                root = 'content' if key == 'stock' or key.startswith('axis:') else 'code'
                relative = Path(record['selected']).relative_to(saved_roots[root])
                current = self.content if root == 'content' else self.code
                try:
                    path = current / relative
                    if (path.resolve(strict=True) != Path(record['resolved'])
                            or _sha(_safe_read(current, relative.as_posix())) != peer['source_versions'][key]):
                        flags.append('valuation_source_changed')
                except (OSError, ValueError):
                    flags.append('valuation_source_unavailable')
        external_name = f'external/{ticker}.json'
        frozen_external = document['inputs'].get(external_name)
        if frozen_external is not None:
            consumed.add(external_name)
            frozen_external = _load_json(base64.b64decode(frozen_external, validate=True))
            if valuation_runner._external_digest(frozen_external) != output.get('external_inputs_digest'):
                return None
            if ticker not in self.external_sources:
                flags.append('external_history_source_unavailable')
            else:
                try:
                    current = _load_json(self._external_bytes(ticker))
                    if valuation_runner._external_digest(current) != output['external_inputs_digest']:
                        flags.append('external_history_source_changed')
                except (OSError, ValueError, KeyError, TypeError):
                    flags.append('external_history_source_unavailable')
        elif output.get('external_inputs_digest') is not None:
            return None
        elif ticker in self.external_sources:
            flags.append('external_history_source_changed')
        if consumed != set(document['inputs']):
            return None
        return tuple(dict.fromkeys(flags))

    def source_conditions(self, ticker: str, draft: Draft) -> tuple[str, ...] | None:
        """Describe current sources without discarding a valid frozen history.

        None means the frozen data itself is invalid. Individual condition
        codes describe availability or change; no code asserts fresh research.
        """
        try:
            document = _load_json(draft._document)
            _validate_draft(document)
            if document['output']['ticker'] != ticker:
                return None
            if isinstance(document['runtime'], dict) and document['runtime'].get('source') == 'existing_runner_output':
                return self._runner_result_conditions(ticker, document)
            name = f'external/{ticker}.json'
            snapshot = dict(document['inputs'])
            frozen = snapshot.pop(name, None)
            configured = ticker in self.external_sources
            if frozen is not None:
                raw = base64.b64decode(frozen, validate=True)
                expected = valuation_runner._external_digest(_load_json(raw))
                if expected is None or document['output'].get('external_inputs_digest') != expected:
                    return None
            elif document['output'].get('external_inputs_digest') is not None:
                return None
            flags = []
            try:
                if document['output']['currency'] != self.currency(ticker):
                    flags.append('valuation_source_changed')
            except (KeyError, ValueError):
                flags.append('valuation_source_unavailable')
            try:
                files = self._snapshot(ticker, include_external=False)
                if snapshot != {key: base64.b64encode(data).decode('ascii') for key, data in files.items()}:
                    flags.append('valuation_source_changed')
            except (OSError, ValueError, KeyError, TypeError):
                flags.append('valuation_source_unavailable')
            runtime = document['runtime'] if isinstance(document['runtime'], dict) else {}
            saved_roots = runtime.get('roots')
            current_roots = self._roots(ticker)
            common = ('code', 'content', 'server', 'code_selected', 'content_selected')
            if not isinstance(saved_roots, dict) or 'parameters' not in runtime:
                flags.append('valuation_source_unavailable')
            elif (any(saved_roots.get(key) != current_roots.get(key) for key in common)
                  or runtime['parameters'] != self.parameters):
                flags.append('valuation_source_changed')
            try:
                if (runtime['runtime_fingerprint'] != self._runtime_generation()
                        or runtime['bwrap_sha256'] != self._bwrap_generation()):
                    flags.append('valuation_runtime_changed')
            except Exception:  # noqa: BLE001 — runtime failure must not erase frozen history
                flags.append('valuation_runtime_unavailable')
            if frozen is not None:
                old_sources = saved_roots.get('external_sources') if isinstance(saved_roots, dict) else None
                saved_source = old_sources.get(ticker) if isinstance(old_sources, dict) else None
                current_source = current_roots.get('external_sources', {}).get(ticker)
                if not configured:
                    flags.append('external_history_source_unavailable')
                elif saved_source is None:
                    flags.append('external_history_source_unavailable')
                elif saved_source != current_source:
                    flags.append('external_history_source_changed')
                if configured:
                    try:
                        current = self._external_bytes(ticker)
                    except (OSError, ValueError):
                        flags.append('external_history_source_unavailable')
                    else:
                        if current != raw:
                            flags.append('external_history_source_changed')
            elif configured:
                flags.append('external_history_source_changed')
            return tuple(dict.fromkeys(flags))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def source_status(self, ticker: str, draft: Draft) -> str:
        conditions = self.source_conditions(ticker, draft)
        if conditions is None or any(flag.endswith('_unavailable') for flag in conditions):
            return 'unavailable'
        return 'changed' if conditions else 'match'

    def source_state(self, ticker: str, draft: Draft) -> str:
        conditions = self.source_conditions(ticker, draft)
        if conditions is None:
            return 'invalid'
        if not conditions:
            return 'current'
        if conditions == ('external_history_source_unavailable',):
            return 'external_unavailable'
        return 'changed'

    def current(self, ticker: str, draft: Draft) -> bool:
        return self.source_state(ticker, draft) == 'current'


class SandboxVerifier:
    """Read the runner registry independently; refuse the unclosed runtime."""
    def __init__(self, config: SandboxConfig) -> None:
        self.config = config

    def _inspect_record(self, ticker: str, draft: Draft, execution_ref: str | None) -> None:
        if not isinstance(execution_ref, str):
            raise ProofUnavailable()
        try:
            with sqlite3.connect(self.config.registry_path) as con:
                row = con.execute('SELECT ticker,document,generation,runtime FROM executions WHERE id=?',
                                  (execution_ref,)).fetchone()
            if row is None or row[0] != ticker or bytes(row[1]) != draft._document:
                raise ProofUnavailable()
            document = _load_json(draft._document)
            _validate_draft(document)
            roots = {'code': Path(self.config.code_root).resolve(strict=True),
                     'content': Path(self.config.content_root).resolve(strict=True),
                     'server': Path(__file__).resolve().parent}
            independent = SandboxProvider(self.config)
            for name, encoded in document['inputs'].items():
                prefix, separator, relative = name.partition('/')
                if not separator:
                    raise ProofUnavailable()
                if prefix == 'external':
                    if relative != f'{ticker}.json':
                        raise ProofUnavailable()
                    current = independent._external_bytes(ticker)
                elif prefix in roots:
                    current = _safe_read(roots[prefix], relative)
                else:
                    raise ProofUnavailable()
                if current != base64.b64decode(encoded, validate=True):
                    raise ProofUnavailable()
            # Declaration closure and runtime are re-evaluated from server config;
            # an ordinary matching hash alone does not authorize confirmation.
            if row[2] != independent.generation(ticker) or row[3] != independent._runtime_generation():
                raise ProofUnavailable()
            expected = independent._snapshot(ticker)
            expected_inputs = {name: base64.b64encode(data).decode('ascii') for name, data in expected.items()}
            if (document['inputs'] != expected_inputs
                    or independent._scoped_roots(document['runtime']['roots'], ticker) != independent._roots(ticker)
                    or document['runtime']['parameters'] != independent.parameters
                    or document['runtime']['runtime_fingerprint'] != row[3]):
                raise ProofUnavailable()
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            raise ProofUnavailable() from None

    def require_current(self, ticker: str, draft: Draft, execution_ref: str | None) -> None:
        self._inspect_record(ticker, draft, execution_ref)
        # Runtime/dynamic closure, provenance and coherent generation remain
        # unproven. A record match is not permission to confirm.
        raise ProofUnavailable()


def sandbox_review_service(path: Path, config: SandboxConfig, devices: DeviceStore) -> ReviewService:
    """Server-only composition; no roots or proof switches are accepted over HTTP."""
    provider = SandboxProvider(config)
    service = ReviewService(path, provider, devices)
    service._sandbox_verifier = SandboxVerifier(config)
    return service
