"""Explicit code/content valuation entry; every call uses a fresh interpreter.

The selected repository is trusted code, not a sandbox.  Python and external
packages come from the calling interpreter and are not version-bound here.
"""
from __future__ import annotations

import argparse
import ast
import importlib
import importlib.abc
import importlib.util
import hashlib
import json
import math
import re
import subprocess
import sys
import types
from datetime import date
from pathlib import Path

_TICKER = re.compile(r'^[A-Z0-9]{1,10}\.(?:US|HK|SH|SZ|SG)$')
_NAME = re.compile(r'^[A-Za-z][A-Za-z0-9_]*$')
# These are the currencies understood by the current framework parser.
_CURRENCIES = frozenset({'USD', 'HKD', 'CNY', 'TWD'})
_ISO_DATE = re.compile(r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$')


def _json_digest(value):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    except (TypeError, ValueError) as exc:
        raise ValueError('peer result must be finite JSON data') from exc
    return hashlib.sha256(raw).hexdigest()


def _root(value, label):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError(f'{label} root must be an absolute path without parent traversal')
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError(f'{label} root must be a directory')
    return path, resolved


def _check_roots(selected, targets):
    for name, path in selected.items():
        try:
            current = path.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f'{name} root changed during calculation') from exc
        if current != targets[name]:
            raise ValueError(f'{name} root changed during calculation')


def _check_band(band, label):
    if (not isinstance(band, dict) or not {'L', 'M', 'H'} <= set(band)
            or set(band) - {'L', 'M', 'H', 'unit', 'units'}):
        raise ValueError(f'{label} must contain L/M/H')
    values = [band[key] for key in ('L', 'M', 'H')]
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
           for value in values) or not 0 <= values[0] < values[1] < values[2]:
        raise ValueError(f'{label} must contain finite increasing values')
    if not math.isfinite(values[0] + values[1]) or not math.isfinite(values[1] + values[2]):
        raise ValueError(f'{label} has non-finite midpoints')


def _validate_response(result, ticker):
    if not isinstance(result, dict) or result.get('ticker') != ticker:
        raise ValueError('worker ticker does not match request')
    if result.get('confirmation_status') != 'unconfirmed':
        raise ValueError('worker confirmation status must be unconfirmed')
    if 'confirmed' in result or result.get('confirmation_eligible', False) is not False:
        raise ValueError('worker cannot claim confirmation or eligibility')
    currency = result.get('currency')
    if not isinstance(currency, str) or currency not in _CURRENCIES:
        raise ValueError(f'worker currency is unsupported: {currency!r}')
    required_dates = ('valuation_as_of', 'framework_as_of')
    history_dates = ('historical_financial_basis_as_of', 'historical_financial_statement_as_of',
                     'historical_valuation_as_of', 'historical_research_as_of')
    for key in required_dates + history_dates:
        if key not in result and key in required_dates:
            raise ValueError(f'worker {key} must be a string or null')
        if key not in result:
            continue
        if result[key] is not None and not isinstance(result[key], str):
            raise ValueError(f'worker {key} must be a string or null')
        value = result[key]
        if value is not None:
            if not _ISO_DATE.fullmatch(value):
                raise ValueError(f'worker {key} must be an ISO date')
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError(f'worker {key} is not a real date') from exc
    units = result.get('units')
    valid_price_units = {f'{currency}_PER_SHARE', f'{currency}_PER_UNIT', f'{currency}_PER_FUND_UNIT'}
    price_units = []
    if units is not None:
        if not isinstance(units, dict):
            raise ValueError('units must be an object or null')
        if 'currency' in units and units['currency'] != currency:
            raise ValueError('units currency conflicts with result currency')
        for key in ('price', 'anchors', 'candidate', 'option'):
            if key in units and (not isinstance(units[key], str) or units[key] not in valid_price_units):
                raise ValueError(f'{key} unit conflicts with currency or per-unit semantics')
        price_units.extend(units[key] for key in ('price', 'anchors', 'candidate', 'option') if key in units)
    _check_band(result.get('anchors'), 'anchors')
    for key in ('candidate', 'option'):
        if key not in result:
            raise ValueError(f'worker {key} is missing')
        if result[key] is not None:
            if not isinstance(result[key], dict):
                raise ValueError(f'worker {key} must be an object or null')
            status = result[key].get('status')
            if status is not None and (not isinstance(status, str) or not status.strip()):
                raise ValueError(f'{key} status is invalid')
            if result[key].get('status') == 'unavailable':
                if result[key].get('anchors') is not None:
                    raise ValueError(f'unavailable {key} cannot contain anchors')
                label = result[key].get('label')
                if label is not None and (not isinstance(label, str) or not label.strip()):
                    raise ValueError(f'{key} label is invalid')
                continue
            _check_band(result[key].get('anchors'), key)
            if key == 'candidate' and (not isinstance(result[key].get('label'), str) or not result[key]['label']):
                raise ValueError('candidate label is missing')
            if key == 'option' and 'status' in result[key] and (
                    not isinstance(result[key]['status'], str) or not result[key]['status'].strip()):
                raise ValueError('option status is invalid')
    flags = result.get('review_flags', [])
    if not isinstance(flags, list) or any(not isinstance(flag, str) or not flag.strip() for flag in flags):
        raise ValueError('review flags are invalid')
    fund_status = result.get('fund_reference_status')
    if fund_status is not None and (not isinstance(fund_status, str) or not fund_status.strip()):
        raise ValueError('fund reference status is invalid')
    band_objects = [result['anchors']]
    for key in ('candidate', 'option'):
        if result[key] is not None:
            band_objects.append(result[key])
            if result[key].get('anchors') is not None:
                band_objects.append(result[key]['anchors'])
    for band in band_objects:
        if 'unit' in band:
            price_units.append(band['unit'])
        if 'units' in band:
            band_units = band['units']
            if not isinstance(band_units, dict) or set(band_units) != {'price'}:
                raise ValueError('band units require a price')
            price_units.append(band_units['price'])
    if any(not isinstance(unit, str) or unit not in valid_price_units for unit in price_units):
        raise ValueError('explicit price unit is invalid')
    if len(set(price_units)) > 1:
        raise ValueError('explicit price units differ; equivalence is not defined')
    if 'basis_changed' not in result or result['basis_changed'] is not None:
        raise ValueError('worker full basis change status must be unknown')
    if not isinstance(result.get('partial_basis_changed'), bool):
        raise ValueError('worker partial basis change indicator is missing')
    if not isinstance(result.get('source_versions'), dict) or not result['source_versions']:
        raise ValueError('worker source versions are missing')
    paths = result.get('source_paths')
    if not isinstance(paths, dict) or set(paths) != set(result['source_versions']):
        raise ValueError('worker source paths are missing')
    metadata = result.get('axis_metadata', {})
    if (not isinstance(metadata, dict)
            or any(not isinstance(alias, str) or f'axis:{alias}' not in result['source_versions']
                   or not isinstance(value, dict) for alias, value in metadata.items())):
        raise ValueError('worker axis metadata must match a declared axis source')
    peers, digests = result.get('peer_snapshots', {}), result.get('peer_result_digests', {})
    if not isinstance(peers, dict) or not isinstance(digests, dict) or set(peers) != set(digests):
        raise ValueError('peer snapshots and digests must have matching aliases')
    if any(not isinstance(alias, str) or not _NAME.fullmatch(alias) or _json_digest(peer) != digests[alias]
           for alias, peer in peers.items()):
        raise ValueError('peer snapshot digest mismatch')
    missing = result.get('peer_missing', [])
    if not isinstance(missing, list) or any(not isinstance(alias, str) for alias in missing) or len(set(missing)) != len(missing):
        raise ValueError('peer missing status must contain unique aliases')


def check_source_identity(result: dict, *, external_inputs=None) -> None:
    """Recheck sources and supplied history; this never authorizes confirmation."""
    if result.get('confirmation_status') != 'unconfirmed':
        raise ValueError('source identity requires an unconfirmed result')
    if result.get('external_inputs_digest') != _external_digest(external_inputs):
        raise ValueError('external inputs differ from the calculation snapshot')
    identity = result['root_identity']
    selected = {name: Path(identity[f'{name}_selected']) for name in ('code', 'content')}
    targets = {name: Path(identity[f'{name}_resolved']) for name in ('code', 'content')}
    _check_roots(selected, targets)
    paths, versions = result['source_paths'], result['source_versions']
    if set(paths) != set(versions):
        raise ValueError('source paths and versions differ')
    ticker = result.get('ticker')
    if not isinstance(ticker, str) or not _TICKER.fullmatch(ticker):
        raise ValueError('invalid ticker in source identity')
    stock_path = targets['content'] / 'eval' / f'{ticker}.py'
    try:
        if not stock_path.resolve(strict=True).is_relative_to(targets['content']):
            raise ValueError('stock source lies outside its selected root')
        source = stock_path.read_bytes()
        libraries, axes, _ = _declaration(source)
        peers = _peer_declaration(source)
    except OSError as exc:
        raise ValueError('stock source changed since calculation') from exc
    expected = {'runner': targets['code'] / 'eval/lib/valuation_runner.py',
                'lib_init': targets['code'] / 'eval/lib/__init__.py', 'stock': stock_path}
    expected.update({f'{name}.py': targets['code'] / 'eval/lib' / f'{name}.py' for name in libraries})
    expected.update({f'axis:{alias}': targets['content'] / 'axes' / f'{name}.py' for alias, name in axes.items()})
    if set(paths) != set(expected):
        raise ValueError('reported source dependencies do not match the stock declaration')
    for key, record in paths.items():
        try:
            if not isinstance(key, str) or not isinstance(record, dict) or set(record) != {'selected', 'resolved'}:
                raise ValueError('invalid source path record')
            if not all(isinstance(record[name], str) for name in ('selected', 'resolved')):
                raise ValueError('source paths must be strings')
            if not isinstance(versions[key], str) or not re.fullmatch(r'[0-9a-f]{64}', versions[key]):
                raise ValueError(f'{key} source digest is invalid')
            path = Path(record['selected'])
            target = Path(record['resolved'])
            if not path.is_absolute() or not target.is_absolute():
                raise ValueError(f'{key} source path is not absolute')
            if path != expected[key]:
                raise ValueError(f'{key} source path differs from the stock declaration')
            root = targets['content'] if key == 'stock' or key.startswith('axis:') else targets['code']
            if not path.is_relative_to(root) or not target.is_relative_to(root):
                raise ValueError(f'{key} source lies outside its selected root')
            if path.resolve(strict=True) != target or hashlib.sha256(path.read_bytes()).hexdigest() != versions[key]:
                raise ValueError(f'{key} source changed since calculation')
        except OSError as exc:
            raise ValueError(f'{key} source changed since calculation') from exc
    snapshots = result.get('peer_snapshots', {})
    digests = result.get('peer_result_digests', {})
    missing = result.get('peer_missing', [])
    if (not isinstance(snapshots, dict) or not isinstance(digests, dict) or not isinstance(missing, list)
            or set(digests) != set(snapshots) or not set(snapshots) <= set(peers)
            or set(missing) != set(peers) - set(snapshots)):
        raise ValueError('peer snapshots do not match the stock declaration')
    for alias in snapshots:
        expected_ticker = peers[alias]
        peer = snapshots[alias]
        if not isinstance(peer, dict) or peer.get('ticker') != expected_ticker:
            raise ValueError(f'peer {alias} ticker does not match declaration')
        if peer.get('peer_snapshots'):
            raise ValueError('nested peer snapshots are not yet supported')
        if peer.get('external_inputs_digest') is not None:
            raise ValueError('peer external input snapshots are not yet source-bound')
        _validate_response(peer, expected_ticker)
        if _json_digest(peer) != digests[alias]:
            raise ValueError(f'peer {alias} snapshot digest mismatch')
        peer_roots = peer.get('root_identity', {})
        if any(peer_roots.get(f'{name}_resolved') != str(targets[name]) for name in ('code', 'content')):
            raise ValueError(f'peer {alias} was computed against different roots')
        check_source_identity(peer)
    _check_roots(selected, targets)


def _external_digest(data) -> str | None:
    """Hash explicit JSON data supplied by the caller, without reading a cache."""
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ValueError('external inputs must be a mapping or null')

    def check(value):
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise ValueError('external input keys must be strings')
            for child in value.values():
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)
        elif value is None or isinstance(value, (str, bool, int)):
            return
        elif isinstance(value, float) and math.isfinite(value):
            return
        else:
            raise ValueError('external inputs must contain finite JSON values')

    check(data)
    canonical = json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def run(code_root: Path, content_root: Path, ticker: str, *, rate_inputs=None, external_inputs=None, peer_results=None) -> dict:
    """Calculate against explicit roots; returned data is never confirmed."""
    if not isinstance(ticker, str) or not _TICKER.fullmatch(ticker):
        raise ValueError('invalid ticker')
    code_path, code_target = _root(code_root, 'code')
    content_path, content_target = _root(content_root, 'content')
    selected = {'code': code_path, 'content': content_path}
    targets = {'code': code_target, 'content': content_target}
    runner = code_target / 'eval/lib/valuation_runner.py'
    if not runner.is_file():
        raise ValueError('selected code root lacks valuation_runner.py')
    if not runner.resolve(strict=True).is_relative_to(code_target):
        raise ValueError('runner source lies outside its selected root')
    runner_digest = hashlib.sha256(runner.read_bytes()).hexdigest()
    peers = {} if peer_results is None else peer_results
    if not isinstance(peers, dict):
        raise ValueError('peer results must be a mapping')
    # JSON copying prevents caller mutation from changing the requested snapshot.
    try:
        peers = json.loads(json.dumps(peers, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError('peer results must be finite JSON data') from exc
    stock_path = content_target / 'eval' / f'{ticker}.py'
    # Let the worker report an unknown stock; the parent must still validate a
    # worker response even if the expected source was absent at preflight.
    declared = _peer_declaration(stock_path.read_bytes()) if stock_path.is_file() else {}
    if not set(peers) <= set(declared):
        raise ValueError('supplied peers do not match the stock declaration')
    for alias in peers:
        expected_ticker = declared[alias]
        peer = peers[alias]
        if not isinstance(peer, dict) or peer.get('ticker') != expected_ticker:
            raise ValueError(f'peer {alias} ticker does not match declaration')
        _validate_response(peer, expected_ticker)
        check_source_identity(peer)
    external_digest = _external_digest(external_inputs)
    request = json.dumps({'ticker': ticker, 'rate_inputs': rate_inputs, 'peer_results': peers,
                          'external_inputs': external_inputs, 'external_inputs_digest': external_digest,
                          'runner_digest': runner_digest,
                          'targets': {key: str(value) for key, value in targets.items()}}, allow_nan=False)
    try:
        child = subprocess.run([sys.executable, '-I', '-B', str(runner), '--_worker',
                                str(code_path), str(content_path)], input=request,
                               text=True, capture_output=True, cwd=code_target, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise ValueError('valuation worker timed out') from exc
    _check_roots(selected, targets)
    if child.returncode:
        raise ValueError(child.stderr.strip() or 'valuation worker failed')
    try:
        result = json.loads(child.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError('worker output is not valid JSON') from exc
    _validate_response(result, ticker)
    if result.get('external_inputs_digest') != external_digest:
        raise ValueError('worker external input digest does not match the request')
    result['source_reconciliation_required'] = True
    result['confirmation_eligible'] = False
    result['root_identity'] = {'code_selected': str(code_path), 'code_resolved': str(code_target),
                               'content_selected': str(content_path), 'content_resolved': str(content_target)}
    result['runtime'] = {'executable': sys.executable, 'environment_bound': False}
    result['confirmation_status'] = 'unconfirmed'
    check_source_identity(result, external_inputs=external_inputs)
    return result


def _declaration_data(source):
    tree = ast.parse(source)
    found = []
    for item in tree.body:
        if isinstance(item, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'DEPENDENCIES' for t in item.targets):
            found.append(item.value)
    if len(found) != 1:
        raise ValueError('stock must declare DEPENDENCIES exactly once')
    try:
        declaration = ast.literal_eval(found[0])
    except (ValueError, TypeError) as exc:
        raise ValueError('DEPENDENCIES must be static data') from exc
    if not isinstance(declaration, dict) or not {'libraries', 'axes'} <= set(declaration) or set(declaration) - {'libraries', 'axes', 'axis_options', 'peers'}:
        raise ValueError('DEPENDENCIES must declare libraries and axes')
    return declaration


def _declaration(source):
    declaration = _declaration_data(source)
    libraries, axes = declaration['libraries'], declaration['axes']
    if not isinstance(libraries, (tuple, list)) or len(set(libraries)) != len(libraries):
        raise ValueError('libraries must be a unique list')
    if not isinstance(axes, dict):
        raise ValueError('axes must be an alias-to-module mapping')
    for name in list(libraries) + list(axes) + list(axes.values()):
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError(f'invalid dependency name: {name!r}')
    options = declaration.get('axis_options', {})
    if not isinstance(options, dict) or not set(options) <= set(axes):
        raise ValueError('axis options must name declared axes')
    for kwargs in options.values():
        if not isinstance(kwargs, dict) or any(not isinstance(key, str) or not _NAME.fullmatch(key) for key in kwargs):
            raise ValueError('axis options must be keyword mappings')
    return libraries, axes, options


def _peer_declaration(source):
    peers = _declaration_data(source).get('peers', {})
    if not isinstance(peers, dict):
        raise ValueError('peers must map aliases to tickers')
    for alias, ticker in peers.items():
        if not isinstance(alias, str) or not _NAME.fullmatch(alias) or not isinstance(ticker, str) or not _TICKER.fullmatch(ticker):
            raise ValueError('invalid peer alias or ticker')
    return peers


def _module(name: str, source: bytes, path: Path, *, package=False, execute=True):
    module = types.ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = name if package else name.rpartition('.')[0]
    if package:
        module.__path__ = [str(path.parent)]
    sys.modules[name] = module
    if execute:
        # Duplicate literal keys disappear during dict construction; inspect
        # the frozen source before running it so they cannot silently win.
        for node in ast.walk(ast.parse(source, filename=str(path))):
            if not isinstance(node, ast.Dict):
                continue
            keys = [key.value for key in node.keys if isinstance(key, ast.Constant)]
            if len(keys) != len(set(keys)):
                raise ValueError(f'{path.name}: duplicate literal mapping key')
        exec(compile(source, str(path), 'exec'), module.__dict__)
    return module


class _SnapshotLoader(importlib.abc.Loader):
    def __init__(self, source, path):
        self.source, self.path = source, path

    def exec_module(self, module):
        module.__file__ = str(self.path)
        exec(compile(self.source, str(self.path), 'exec'), module.__dict__)


class _DeclaredLibraries(importlib.abc.MetaPathFinder):
    def __init__(self, libraries, sources, targets):
        self.libraries, self.sources, self.targets = set(libraries), sources, targets

    def find_spec(self, fullname, path, target=None):
        if not fullname.startswith('lib.'):
            return None
        name = fullname.removeprefix('lib.')
        if name not in self.libraries:
            raise ModuleNotFoundError(f'undeclared repository library: {fullname}')
        key = f'{name}.py'
        return importlib.util.spec_from_loader(fullname, _SnapshotLoader(self.sources[key], self.targets[key]))


def _worker(code_root: Path, content_root: Path, request: dict) -> dict:
    ticker = request['ticker']
    if not isinstance(ticker, str) or not _TICKER.fullmatch(ticker):
        raise ValueError('invalid ticker')
    external_inputs = request.get('external_inputs')
    external_digest = _external_digest(external_inputs)
    if external_digest != request.get('external_inputs_digest'):
        raise ValueError('worker external input digest does not match request')
    selected = {'code': code_root, 'content': content_root}
    targets_root = {key: Path(value) for key, value in request['targets'].items()}
    _check_roots(selected, targets_root)
    code_target, content_target = targets_root['code'], targets_root['content']
    paths = {'runner': code_target / 'eval/lib/valuation_runner.py',
             'lib_init': code_target / 'eval/lib/__init__.py',
             'stock': content_target / 'eval' / f'{ticker}.py'}
    if not paths['stock'].is_file():
        raise ValueError(f'unknown ticker: {ticker}')
    stock_target = paths['stock'].resolve(strict=True)
    if not stock_target.is_relative_to(content_target):
        raise ValueError('stock source lies outside its selected root')
    stock_source = stock_target.read_bytes()
    libraries, axes, axis_options = _declaration(stock_source)
    peers = _peer_declaration(stock_source)
    for name in libraries:
        paths[f'{name}.py'] = code_target / 'eval/lib' / f'{name}.py'
    for alias, name in axes.items():
        paths[f'axis:{alias}'] = content_target / 'axes' / f'{name}.py'
    targets = {key: path.resolve(strict=True) for key, path in paths.items()}
    for key, target in targets.items():
        root = content_target if key == 'stock' or key.startswith('axis:') else code_target
        if not target.is_relative_to(root):
            raise ValueError(f'{key} source lies outside its selected root')
    sources = {key: path.read_bytes() for key, path in targets.items()}
    if sources['stock'] != stock_source:
        raise ValueError('stock changed while reading declaration')
    digests = {key: hashlib.sha256(source).hexdigest() for key, source in sources.items()}
    if request.get('runner_digest') != digests['runner']:
        raise ValueError('runner changed before worker started; retry')
    sys.meta_path.insert(0, _DeclaredLibraries(libraries, sources, targets))
    _module('lib', sources['lib_init'], targets['lib_init'], package=True)
    shared = {}
    axis_versions = {}
    axis_metadata = {}
    for alias in axes:
        key = f'axis:{alias}'
        module = _module(f'_valuation_axis_{alias}', sources[key], targets[key])
        shared[alias] = module.inputs(ticker, **axis_options.get(alias, {}))
        if not isinstance(shared[alias], dict):
            raise ValueError(f'axis {alias} did not return a mapping')
        axis_versions[alias] = digests[key]
        if 'axis_metadata' in shared[alias]:
            value = shared[alias]['axis_metadata']
            if not isinstance(value, dict):
                raise ValueError(f'axis {alias} metadata must be a mapping')
            # Freeze metadata before passing the axis snapshot to stock code.
            # It stays bound to the same recorded axis source version.
            axis_metadata[alias] = json.loads(json.dumps(value, allow_nan=False))
    supplied_peers = request.get('peer_results', {})
    if not isinstance(supplied_peers, dict) or not set(supplied_peers) <= set(peers):
        raise ValueError('supplied peers do not match the stock declaration')
    peer_snapshots = json.loads(json.dumps(supplied_peers, allow_nan=False))
    peer_digests = {alias: _json_digest(peer) for alias, peer in peer_snapshots.items()}
    stock = _module('_valuation_stock', sources['stock'], targets['stock'])
    context = {'axes': shared, 'axis_versions': axis_versions,
               'libraries': {f'{name}.py': digests[f'{name}.py'] for name in libraries},
               'rate_inputs': request.get('rate_inputs'), 'external_inputs': external_inputs,
               'peers': supplied_peers}
    result = stock.calculate(context)
    if not isinstance(result, dict):
        raise ValueError('stock calculation must return an object')
    if _external_digest(context['external_inputs']) != external_digest:
        raise ValueError('external inputs changed during calculation')
    reserved = {'confirmed', 'confirmation_status', 'confirmation_eligible', 'axis_metadata', 'external_inputs_digest',
                'peer_snapshots', 'peer_result_digests', 'peer_missing',
                'source_reconciliation_required'} & result.keys()
    if reserved:
        raise ValueError(f'stock cannot supply confirmation fields: {sorted(reserved)}')
    allowed = {f'lib.{name}' for name in libraries}
    untracked = sorted(name for name in sys.modules if name.startswith('lib.') and name not in allowed)
    if untracked:
        raise ValueError(f'untracked repository library dependencies: {untracked}')
    for key, path in paths.items():
        if path.resolve(strict=True) != targets[key] or hashlib.sha256(path.read_bytes()).hexdigest() != digests[key]:
            raise ValueError(f'{key} changed during calculation; retry')
    if {alias: _json_digest(peer) for alias, peer in supplied_peers.items()} != peer_digests:
        raise ValueError('peer result changed during calculation')
    _check_roots(selected, targets_root)
    result['axis_metadata'] = axis_metadata
    result['peer_snapshots'] = peer_snapshots
    result['peer_result_digests'] = peer_digests
    result['peer_missing'] = sorted(set(peers) - set(peer_snapshots))
    result['external_inputs_digest'] = external_digest
    result['source_versions'] = digests
    result['source_paths'] = {key: {'selected': str(path), 'resolved': str(targets[key])}
                              for key, path in paths.items()}
    result['confirmation_status'] = 'unconfirmed'
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--code-root', type=Path)
    parser.add_argument('--content-root', type=Path)
    parser.add_argument('--ticker')
    parser.add_argument('--peer-results', type=Path)
    parser.add_argument('--external-inputs', type=Path)
    parser.add_argument('--_worker', nargs=2, metavar=('CODE_ROOT', 'CONTENT_ROOT'))
    args = parser.parse_args()
    try:
        if args._worker:
            result = _worker(Path(args._worker[0]), Path(args._worker[1]), json.load(sys.stdin))
        else:
            if args.code_root is None or args.content_root is None or args.ticker is None:
                raise ValueError('code root, content root and ticker are required')
            peers = None if args.peer_results is None else json.loads(args.peer_results.read_text(encoding='utf-8'))
            supplied = (None if args.external_inputs is None else
                        json.loads(args.external_inputs.read_text(encoding='utf-8')))
            result = run(args.code_root, args.content_root, args.ticker, peer_results=peers, external_inputs=supplied)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (ValueError, KeyError, OSError, ZeroDivisionError, TypeError, ImportError, SyntaxError) as exc:
        print(f'FAIL: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
