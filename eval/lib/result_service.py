"""Open the existing valuation result store for read-only consumers.

The explicit database path is shared by the app, daily digest and index.
Reading frozen results does not initialize a calculator or a runtime.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys

from eval.lib.external_history import read_history


class _UnknownSources:
    """A frozen result is readable even if live sources cannot be checked."""
    def generation(self, ticker):
        raise OSError('source configuration unavailable')

    def currency(self, ticker):
        raise OSError('source configuration unavailable')

    def source_status(self, ticker, draft):
        return 'unavailable'


class _UnavailableRuntime:
    files = ()
    python_version = 'unavailable'

    def check(self):
        raise OSError('runtime configuration unavailable')


class _Unavailable:
    def current(self, ticker):
        return {'status': 'unavailable'}


class _CheckInputSources:
    """Lazy per-ticker inputs from the existing result-source configuration."""

    def __init__(self, external_sources, tickers):
        self.external_sources = external_sources
        self.tickers = tickers
        self.parents = {}
        for ticker, path in external_sources.items():
            try:
                if isinstance(path, str):
                    self.parents[ticker] = Path(path).parent.resolve(strict=False)
            except (OSError, ValueError):
                pass  # Report this ticker when it is checked, not the whole pool.

    def for_ticker(self, ticker, overrides):
        from eval.lib.valuation_drafts import _load_json

        result = dict(overrides)
        if 'external_inputs' not in result and ticker in self.external_sources:
            try:
                if ticker not in self.tickers:
                    raise ValueError('external source ticker is not configured')
                value = self.external_sources[ticker]
                if not isinstance(value, str):
                    raise ValueError('external source path must be a string')
                path = Path(value)
                history = _load_json(read_history(path, self.parents[ticker]))
                result['external_inputs'] = history
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise ValueError(f'external inputs for {ticker} unavailable: {exc}') from exc
        return result


def _read_source_configuration(config_path):
    from eval.lib.valuation_drafts import _load_json

    path = Path(config_path)
    try:
        if not path.is_absolute() or not path.is_file():
            raise ValueError('source configuration must be an existing absolute file')
        config = _load_json(path.read_bytes())
        if not isinstance(config, dict):
            raise ValueError('source configuration must be a JSON object')
        external = config.get('external_sources')
        if external is None:
            external = {}
        if not isinstance(external, dict) or any(not isinstance(key, str) for key in external):
            raise ValueError('external_sources must map tickers to paths')
        return config, external
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError(f'valuation source configuration unavailable: {exc}') from exc


def check_input_sources(config_path, *, code_root=None, content_root=None):
    """Read configured check inputs without invoking the calculator or server."""
    if not config_path:
        return _CheckInputSources({}, {})
    config, external = _read_source_configuration(config_path)
    for key, expected in (('code_root', code_root), ('content_root', content_root)):
        value = config.get(key)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError(f'{key} must be an absolute directory')
        try:
            actual = Path(value).resolve(strict=True)
            if not actual.is_dir() or (expected is not None and actual != Path(expected).resolve(strict=True)):
                raise ValueError(f'{key} differs from the check workspace')
        except OSError as exc:
            raise ValueError(f'{key} is unavailable') from exc
    tickers = config.get('tickers')
    if not isinstance(tickers, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                            for k, v in tickers.items()):
        raise ValueError('tickers must map stock identifiers to currencies')
    return _CheckInputSources(external, tickers)


class _SafeReader:
    def __init__(self, service, source_factory=None):
        self.service = service
        self.source_factory = source_factory

    def current(self, ticker):
        try:
            service = self.service
            if self.source_factory is not None:
                # One complete provider snapshot per read: a repaired config
                # takes effect without mutating a shared service across requests.
                service = type(service).open_readonly(service.path, self.source_factory())
            return service.current_for_display(ticker)
        except Exception:  # noqa: BLE001 — a damaged store is not an empty store
            return {'status': 'unavailable'}


def _configured_sources():
    config_path = os.environ.get('LYNCH_VALUATION_SOURCES')
    if not config_path or not Path(config_path).is_absolute():
        return _UnknownSources()
    try:
        from lynch_reader.sandbox_valuation import RuntimeSnapshot, SandboxConfig, SandboxProvider
        config, _ = _read_source_configuration(config_path)
        code, content = Path(config['code_root']), Path(config['content_root'])
        if not code.is_absolute() or not content.is_absolute():
            raise ValueError('source roots must be absolute')
        runtime_config = config.get('runtime')
        if runtime_config is None:
            runtime = _UnavailableRuntime()
        else:
            runtime_root = Path(runtime_config['root'])
            if not runtime_root.is_absolute():
                raise ValueError('runtime root must be absolute')
            runtime = RuntimeSnapshot(runtime_root, tuple(tuple(pair) for pair in runtime_config['files']),
                                      runtime_config['python_version'])
        selected = SandboxConfig(code_root=code, content_root=content, runtime=runtime,
                                 work_root=Path('.'), registry_path=Path('.'), tickers=config['tickers'],
                                 rate_inputs=config.get('rate_inputs'), external_sources=config.get('external_sources'))
        return SandboxProvider(selected, read_only=True)
    except Exception:  # noqa: BLE001 — result store remains readable if source config is unavailable
        return _UnknownSources()


def open_result_reader(*, database: Path | None = None, source_provider=None):
    """Return a read-only reader; a missing configuration is explicitly unavailable."""
    configured = database if database is not None else os.environ.get('LYNCH_VALUATION_DB')
    if not configured:
        return _Unavailable()
    path = Path(configured)
    if not path.is_absolute():
        return _Unavailable()
    server = Path(__file__).resolve().parents[2] / 'app' / 'server'
    if str(server) not in sys.path:
        sys.path.insert(0, str(server))
    from lynch_reader.valuation_reviews import ReviewService
    provider = source_provider if source_provider is not None else _UnknownSources()
    return _SafeReader(ReviewService.open_readonly(path, provider),
                       source_factory=_configured_sources if source_provider is None else None)
