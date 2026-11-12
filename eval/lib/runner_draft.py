"""Freeze a previously produced runner output; this does not execute or confirm it."""
import base64
import hashlib
from pathlib import Path

from eval.lib.valuation_drafts import Draft, MAX_RECORD_BYTES, _json_bytes, _load_json, _validate_draft
from eval.lib.valuation_runner import _validate_response, check_source_identity


def draft_from_result(output, *, external_inputs=None):
    # Copy JSON data first so callers cannot mutate it while sources are read.
    output = _load_json(_json_bytes(output))
    external_inputs = None if external_inputs is None else _load_json(_json_bytes(external_inputs))
    ticker = output['ticker']
    _validate_response(output, ticker)
    check_source_identity(output, external_inputs=external_inputs)
    roots = {name: Path(output['root_identity'][f'{name}_resolved']) for name in ('code', 'content')}
    snapshot = {}
    total = 0
    for key, record in output['source_paths'].items():
        root = 'content' if key == 'stock' or key.startswith('axis:') else 'code'
        source = Path(record['selected'])
        name = root + '/' + source.relative_to(roots[root]).as_posix()
        if name in snapshot:
            raise ValueError('multiple source roles refer to the same snapshot path')
        data = source.read_bytes()
        total += len(data)
        if total > MAX_RECORD_BYTES:
            raise ValueError('source snapshot is too large')
        if hashlib.sha256(data).hexdigest() != output['source_versions'][key]:
            raise ValueError(f'{key} source changed during snapshot')
        snapshot[name] = base64.b64encode(data).decode('ascii')
    if external_inputs is not None:
        snapshot[f'external/{ticker}.json'] = base64.b64encode(_json_bytes(external_inputs)).decode('ascii')
    check_source_identity(output, external_inputs=external_inputs)
    document = {'format': 1, 'entry': f'content/eval/{ticker}.py', 'inputs': snapshot,
                'runtime': {'source': 'existing_runner_output', 'execution_verified': False},
                'output': output}
    _validate_draft(document)
    encoded = _json_bytes(document)
    if len(encoded) > MAX_RECORD_BYTES:
        raise ValueError('draft is too large')
    return Draft(encoded)
