"""Project saved axis metadata without reading human documents or running models."""
from datetime import date
import json
import re


def _display_note(note):
    if not isinstance(note, str) or not note.strip():
        raise ValueError('invalid consensus source note')
    if note.lstrip().startswith('>'):
        match = re.fullmatch(r'\s*>\s*共识[：:]\s*\S+?\s*[（(](.*)[）)]\s*', note)
        if not match:
            return None
        note = re.sub(r'\s*[；;，,]?\s*as-of\s*\d{4}-\d{2}-\d{2}\s*$', '', match.group(1), flags=re.I).strip()
    return note or None


def _axis(ticker, data):
    name = data.get('axis_id')
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', name):
        raise ValueError('invalid axis id')
    if data.get('logical_id') != '_AXIS.' + name:
        raise ValueError('invalid logical id')
    label, title = data.get('label'), data.get('title')
    if any(not isinstance(s, str) or not s.strip() for s in (label, title)):
        raise ValueError('missing display name')
    role = data.get('role')
    if role not in ('priced', 'reference'):
        raise ValueError('unknown role')
    priced, references = data.get('priced_mounts'), data.get('reference_mounts')
    if (not isinstance(priced, list) or not isinstance(references, list)
            or any(not isinstance(t, str) or not t for t in priced + references)
            or set(priced) & set(references)
            or ticker not in (priced if role == 'priced' else references)):
        raise ValueError('invalid membership')
    # A reference mount can carry the axis-level observation, but it is not
    # this stock's own scenario consensus and must not be inherited.
    consensus = data.get('historical_consensus') if role == 'priced' else None
    if consensus is not None:
        if not isinstance(consensus, dict):
            raise ValueError('invalid consensus')
        tier, day = consensus.get('band'), consensus.get('as_of')
        if tier not in ('<L', 'L', 'L-M', 'L–M', 'M', 'M-H', 'M–H', 'H', '>H'):
            raise ValueError('invalid consensus band')
        if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
            raise ValueError('invalid consensus date')
        consensus = {'tier': tier.replace('-', '–'), 'as_of': day,
                     'kind': consensus.get('kind', 'historical')}
        if consensus['kind'] not in ('historical', 'single_institution'):
            raise ValueError('invalid consensus kind')
        if consensus['kind'] == 'single_institution':
            institution = data['historical_consensus'].get('institution')
            if not isinstance(institution, str) or not institution.strip():
                raise ValueError('missing reference institution')
            consensus['institution'] = institution
        note = data['historical_consensus'].get('source_note')
        if note is not None:
            consensus['source_note'] = _display_note(note)
    return {'name': name, 'label': label, 'title': title, 'role': role, 'consensus': consensus}


def project_axes(ticker, record):
    output = record.get('output', {}) if isinstance(record, dict) else {}
    metadata = output.get('axis_metadata') if isinstance(output, dict) else None
    sources = output.get('source_versions') if isinstance(output, dict) else None
    supplement = record.get('axis_supplement') if isinstance(record, dict) else None
    incomplete = False
    own = output.get('own_consensus') if isinstance(output, dict) else None
    own_from_supplement = False
    if metadata is None and isinstance(supplement, dict):
        metadata, sources = supplement.get('axis_metadata'), supplement.get('source_versions')
        incomplete = supplement.get('complete') is not True
        if own is None:
            own = supplement.get('own_consensus')
            own_from_supplement = own is not None
    if not isinstance(metadata, dict) or not isinstance(sources, dict):
        metadata, sources, incomplete = {}, {}, True
    declared = {key[5:] for key in sources if isinstance(key, str) and key.startswith('axis:')}
    unknown = incomplete or set(metadata) != declared
    axes = []
    for alias in sorted(set(metadata) & declared):
        data = metadata[alias]
        if not isinstance(data, dict):
            unknown = True
            continue
        kind = data.get('axis_type')
        if kind in ('rates_axis', 'peer_ruler_axis'):
            continue
        if kind != 'shared_scenario_axis':
            unknown = True
            continue
        try:
            json.dumps(data, allow_nan=False)
            value = _axis(ticker, data)
            if metadata is not output.get('axis_metadata'):
                value['historical_supplement'] = True
                value['relationship_to_result'] = supplement.get('relationship_to_result', 'unverified')
            if any(axis['name'] == value['name'] for axis in axes):
                unknown = True
            else:
                axes.append(value)
        except (TypeError, ValueError):
            unknown = True
    if own is not None:
        try:
            tier, day = own['tier'], own['as_of']
            if tier not in ('<L', 'L', 'L-M', 'L–M', 'M', 'M-H', 'M–H', 'H', '>H'):
                raise ValueError('invalid own consensus')
            if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
                raise ValueError('invalid own consensus date')
            note = own.get('source_note')
            if note is not None:
                note = _display_note(note)
            kind = own.get('kind', 'historical')
            if kind not in ('historical', 'single_institution'):
                raise ValueError('invalid own consensus kind')
            institution = own.get('institution')
            if kind == 'single_institution' and (not isinstance(institution, str) or not institution.strip()):
                raise ValueError('missing own consensus institution')
            own = {'tier': tier.replace('-', '–'), 'as_of': day, 'kind': kind, 'note': note,
                   'institution': institution}
            if own_from_supplement:
                own['relationship_to_result'] = 'unverified'
        except (KeyError, TypeError, ValueError):
            own, unknown = None, True
    return {'status': 'unknown' if unknown else 'known', 'axes': axes, 'own_consensus': own}


def _collect_axes(projections):
    """Aggregate per-result relationships without borrowing another result's consensus."""
    names = {}
    conflicts = set()
    for ticker, projection in projections.items():
        for axis in projection['axes']:
            key = axis['name']
            identity = (axis['label'], axis['title'])
            if key in names and names[key]['identity'] != identity:
                conflicts.add(key)
                continue
            entry = names.setdefault(key, {'identity': identity, 'tickers': [], 'refs': []})
            entry['tickers' if axis['role'] == 'priced' else 'refs'].append(ticker)
    if conflicts:
        for projection in projections.values():
            if any(axis['name'] in conflicts for axis in projection['axes']):
                projection['status'] = 'unknown'
                projection['axes'] = [axis for axis in projection['axes'] if axis['name'] not in conflicts]
    return [{'name': name, 'label': entry['identity'][0], 'title': entry['identity'][1],
             'consensus': None, 'tickers': sorted(entry['tickers']), 'refs': sorted(entry['refs'])}
            for name, entry in sorted(names.items()) if name not in conflicts]


def collect_axes(projections, *, archived=frozenset()):
    active = {ticker: value for ticker, value in projections.items() if ticker not in archived}
    old = {ticker: value for ticker, value in projections.items() if ticker in archived}
    active_axes = _collect_axes(active)
    old_axes = _collect_axes(old)
    by_name = {axis['name']: axis for axis in active_axes}
    for axis in old_axes:
        current = by_name.get(axis['name'])
        if current is None:
            by_name[axis['name']] = axis
        elif (current['label'], current['title']) == (axis['label'], axis['title']):
            current['tickers'] = sorted(current['tickers'] + axis['tickers'])
            current['refs'] = sorted(current['refs'] + axis['refs'])
        else:
            for projection in old.values():
                if any(item['name'] == axis['name'] for item in projection['axes']):
                    projection['status'] = 'unknown'
                    projection['axes'] = [item for item in projection['axes'] if item['name'] != axis['name']]
    return [by_name[name] for name in sorted(by_name)]
