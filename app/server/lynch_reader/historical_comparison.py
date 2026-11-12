"""One-time comparison of a frozen migration result with cited historical evidence.

Structured operator annotations are attestations, not proof of execution or of
user identity. Missing fields are unknown, never evidence of absence.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, ROUND_HALF_UP
import math

from eval.lib.valuation_drafts import _load_json


class EvidenceMismatch(ValueError):
    """Historical material makes a claim that conflicts with the migrated result."""


def unreviewed(output, reason='historical_material_unreviewed'):
    """No usable comparison: retain the script result and state what is unknown."""
    warnings = {reason, 'historical_material_unreviewed', 'anchors_unknown', 'currency_unknown',
                'candidate_unknown', 'option_unknown', 'review_flags_unknown'}
    if output.get('option') is not None:
        warnings.add('option_status_unknown')
    if output.get('fund_reference_status') is not None:
        warnings.add('fund_reference_status_unknown')
    return {
        'anchors': {'state': 'unknown', 'source': 'unreviewed', 'data': {}},
        'candidate': {'state': 'unknown', 'source': 'unreviewed'},
        'option': {'state': 'unknown', 'source': 'unreviewed'},
        'currency': {'state': 'unknown', 'value': None, 'source': 'unreviewed', 'source_excerpt': None},
        'precision': {}, 'price_units': {}, 'review_flags_state': 'unknown', 'option_status_state': 'unknown',
        'review_flags': {'state': 'unknown', 'source': 'unreviewed', 'values': None},
        'option_status': {'state': 'unknown', 'source': 'unreviewed', 'value': None},
        'fund_reference_status': {'historical': None, 'current': output.get('fund_reference_status')},
        'differences': [], 'uncertainties': sorted(warnings),
    }


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('historical price is not a finite number')
    return Decimal(str(value))


def _band(value):
    if not isinstance(value, dict) or not {'L', 'M', 'H'} <= set(value):
        raise ValueError('historical price band is incomplete')
    return {tier: value[tier] for tier in ('L', 'M', 'H') if _number(value[tier]) is not None}


def _partial_band(value):
    """Keep observable historical numbers; an omitted or nonnumeric tier stays unknown."""
    if not isinstance(value, dict):
        return {}
    known = {}
    for tier in ('L', 'M', 'H'):
        if tier not in value or value[tier] is None:
            continue
        try:
            _number(value[tier])
        except (ValueError, OverflowError):
            continue
        known[tier] = value[tier]
    return known


def _candidate(value):
    if isinstance(value, dict) and value.get('status') == 'unavailable':
        label = value.get('label')
        if label is not None and (not isinstance(label, str) or not label.strip()):
            raise ValueError('invalid unavailable candidate label')
        result = {'status': 'unavailable', 'label': label}
        anchors = _partial_band(value.get('anchors'))
        if anchors:
            result['anchors'] = anchors
        return result
    if not isinstance(value, dict):
        raise ValueError('historical candidate is invalid')
    result = {}
    if isinstance(value.get('label'), str) and value['label'].strip():
        result['label'] = value['label']
    anchors = _partial_band(value.get('anchors'))
    if anchors:
        result['anchors'] = anchors
    return result


def _option(value):
    if not isinstance(value, dict):
        raise ValueError('historical option is incomplete')
    result = {}
    if value.get('status') == 'unavailable':
        result['status'] = 'unavailable'
    if 'anchors' in value:
        anchors = _partial_band(value['anchors'])
        if anchors:
            result['anchors'] = anchors
    if 'value' in value and value['value'] is not None:
        _number(value['value'])
        result['value'] = value['value']
    if 'value_unit' in value and value['value_unit'] is not None:
        if not isinstance(value['value_unit'], str) or not value['value_unit'].strip():
            raise ValueError('invalid historical option value unit')
        result['value_unit'] = value['value_unit']
    return result


def _state(container, key, convert, source_prefix=''):
    if key not in container:
        return {'state': 'unknown', 'source': 'missing_field'}
    if container[key] is None:
        # Historical null may mean either no band or not yet assessed. Only a
        # cited operator attestation can declare historical absence.
        return {'state': 'unknown', 'source': 'null_semantics_unspecified'}
    try:
        data = convert(container[key])
    except (ValueError, TypeError, OverflowError):
        # An old free-text field is still retained in the frozen source; it
        # does not prove presence or absence of a machine-readable band.
        return {'state': 'unknown', 'source': 'incomplete_field'}
    return {'state': 'present', 'source': 'structured_record', 'source_pointer': source_prefix + '/' + key,
            'data': data}


def _unit_declarations(output, part, *, tolerate_incomplete=False):
    found = []
    top = output.get('units')
    if top is not None:
        if not isinstance(top, dict):
            if tolerate_incomplete:
                return None
            raise ValueError('invalid historical units')
        for key in ('price', part):
            if key in top:
                found.append(top[key])
    value = output.get('anchors') if part == 'anchors' else output.get(part)
    objects = [value]
    if part != 'anchors' and isinstance(value, dict):
        objects.append(value.get('anchors'))
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        if 'unit' in obj:
            found.append(obj['unit'])
        if 'units' in obj:
            units = obj['units']
            if not isinstance(units, dict) or set(units) != {'price'}:
                if tolerate_incomplete:
                    return None
                raise ValueError('invalid historical band units')
            found.append(units['price'])
    if any(not isinstance(unit, str) or not unit.strip() for unit in found):
        if tolerate_incomplete:
            return None
        raise ValueError('invalid historical price unit')
    distinct = tuple(sorted(set(found)))
    return distinct[0] if len(distinct) == 1 else distinct if distinct else None


def _difference(differences, warnings, field, historical, current, kind):
    differences.append({'field': field, 'historical': historical, 'current': current, 'kind': kind})
    warnings.add('historical_values_differ')


def _cited(entry, source_text):
    if not isinstance(entry, dict):
        raise ValueError('manifest entry must be an object')
    excerpt, rationale = entry.get('source_excerpt'), entry.get('rationale')
    if not isinstance(excerpt, str) or not excerpt:
        raise ValueError('manifest source excerpt is not in the historical material')
    start = 0
    while True:
        index = source_text.find(excerpt, start)
        if index < 0:
            raise ValueError('manifest source excerpt is missing or splits a numeric token')
        before = source_text[index - 1] if index else ''
        end = index + len(excerpt)
        after = source_text[end] if end < len(source_text) else ''
        split_before = excerpt[0] in '0123456789.' and before in '0123456789.' and bool(before)
        split_after = excerpt[-1] in '0123456789.' and after in '0123456789.' and bool(after)
        if not split_before and not split_after:
            break
        start = index + 1
    if not isinstance(rationale, str) or not rationale.strip():
        raise ValueError('manifest rationale is required')


def _manifest(raw, ticker, result_hash):
    if raw is None:
        return {}
    from hashlib import sha256
    data = _load_json(raw)
    allowed = {'ticker', 'record_sha256', 'anchors', 'candidate', 'option', 'review_flags', 'option_status', 'precision', 'price_unit', 'currency'}
    if (not isinstance(data, dict) or set(data) - allowed or data.get('ticker') != ticker
            or data.get('record_sha256') != result_hash):
        raise ValueError('operator manifest does not bind the selected historical record')
    # Retain the original bytes separately; hash here only makes malformed
    # non-byte inputs fail rather than accepting arbitrary Python objects.
    sha256(raw).hexdigest()
    return data


def _manifest_part(entry, part, source_text):
    if not isinstance(entry, dict) or entry.get('state') not in ('present', 'absent', 'unknown'):
        raise ValueError('invalid extra-band manifest state')
    state = entry['state']
    if state == 'unknown':
        return {'state': 'unknown', 'source': 'operator_unknown'}
    _cited(entry, source_text)
    if state == 'absent':
        if 'data' in entry:
            raise ValueError('absent band cannot supply a value')
        return {'state': 'absent', 'source': 'operator_attested', 'source_excerpt': entry['source_excerpt'],
                'rationale': entry['rationale']}
    source_data = entry.get('data')
    data = _candidate(source_data) if part == 'candidate' else _option(source_data)
    if part == 'option' and not data:
        raise ValueError('present option needs a cited value or band')
    excerpt = entry['source_excerpt']
    price_unit = _unit_declarations({part: source_data}, part)
    if isinstance(price_unit, tuple):
        raise ValueError('operator-transcribed band has conflicting price units')
    cited_units = [unit for unit in (price_unit, data.get('value_unit')) if unit is not None]
    unit_excerpt = entry.get('unit_source_excerpt', excerpt)
    if cited_units and (not isinstance(unit_excerpt, str) or not unit_excerpt or unit_excerpt not in source_text):
        raise ValueError('band unit source must be cited')
    # Values and units are the operator's cited transcription; the reader
    # stores the exact excerpt but never attempts to interpret prose itself.
    return {'state': 'present', 'source': 'operator_attested', 'source_excerpt': excerpt,
            'rationale': entry['rationale'], 'data': data, 'price_unit': price_unit,
            'unit_source_excerpt': unit_excerpt if cited_units else None}


def _merge_part(inferred, entry, part, source_text):
    if entry is None:
        return inferred
    attested = _manifest_part(entry, part, source_text)
    if inferred['state'] != 'unknown' and attested['state'] != 'unknown':
        if inferred['state'] != attested['state']:
            raise EvidenceMismatch('manifest and historical record disagree about the extra band')
        if inferred['state'] == 'present':
            # Both claims must agree on every overlapping field.
            for key, value in attested['data'].items():
                if key == 'anchors' and key in inferred['data']:
                    if any(tier in inferred['data'][key] and inferred['data'][key][tier] != amount
                           for tier, amount in value.items()):
                        raise EvidenceMismatch('manifest and historical extra band disagree')
                elif key in inferred['data'] and inferred['data'][key] != value:
                    raise EvidenceMismatch('manifest and historical extra band disagree')
            merged = dict(inferred['data'])
            merged.update(attested['data'])
            if 'anchors' in inferred['data'] and 'anchors' in attested['data']:
                merged['anchors'] = {**inferred['data']['anchors'], **attested['data']['anchors']}
            attested['data'] = merged
    if attested['state'] == 'unknown' and inferred['state'] != 'unknown':
        return inferred
    return attested


def _precision_spec(manifest, source_text, path, historical_value):
    specs = manifest.get('precision', {})
    if not isinstance(specs, dict):
        raise ValueError('precision must be a mapping')
    entry = specs.get(path)
    if entry is None:
        return None
    _cited(entry, source_text)
    places = entry.get('decimals')
    if entry.get('mode') != 'displayed' or type(places) is not int or not 0 <= places <= 10:
        raise ValueError('display precision must state its decimal places')
    precision_excerpt = entry.get('precision_source_excerpt')
    if precision_excerpt is not None and (not isinstance(precision_excerpt, str) or precision_excerpt not in source_text):
        raise ValueError('display precision source was not cited')
    return places


def _compare_number(old, new, path, manifest, source_text, inferred_places, warnings, precision,
                    differences, unrounded_fields):
    old_number, new_number = _number(old), _number(new)
    places = _precision_spec(manifest, source_text, path, old)
    basis = 'operator_attested_display' if places is not None else 'unrounded' if path in unrounded_fields else 'precision_unknown'
    if places is not None and path in unrounded_fields:
        _difference(differences, warnings, path + '.precision', 'unrounded', 'operator_attested_display', 'source_conflict')
        warnings.add('precision_source_conflict')
        places, basis = None, 'unrounded'
    if places is None and path in inferred_places:
        places, excerpt = inferred_places[path]
        basis = 'document_display'
    if places is not None:
        quantum = Decimal(1).scaleb(-places)
        try:
            # Without a cited rounding rule, a halfway case must match under
            # both common conventions; choosing one could invent consistency.
            same = all(old_number.quantize(quantum, rounding=mode) == new_number.quantize(quantum, rounding=mode)
                       for mode in (ROUND_HALF_UP, ROUND_HALF_EVEN))
        except InvalidOperation as exc:
            raise ValueError('invalid display precision') from exc
        precision[path] = {'basis': basis, 'decimals': places}
        if basis == 'document_display':
            precision[path]['source_excerpt'] = excerpt
        if basis == 'operator_attested_display':
            warnings.add('display_precision_attested')
            precision[path]['source_excerpt'] = manifest['precision'][path]['source_excerpt']
            precision[path]['precision_source_excerpt'] = manifest['precision'][path].get('precision_source_excerpt')
            precision[path]['rationale'] = manifest['precision'][path]['rationale']
    else:
        same = old_number == new_number
        precision[path] = {'basis': basis}
        if basis == 'precision_unknown':
            warnings.add('precision_unknown')
    if not same:
        kind = ('precision_unknown_difference' if basis == 'precision_unknown' else
                'unrounded_difference' if basis == 'unrounded' else 'display_difference')
        _difference(differences, warnings, path, old, new, kind)


def _known_or_manifest(old, key, entry, source_text):
    known = key in old and old[key] is not None
    value = old.get(key)
    if entry is not None:
        if not isinstance(entry, dict) or entry.get('state') not in ('known', 'unknown'):
            raise ValueError('invalid review-status manifest state')
        if entry['state'] == 'known':
            _cited(entry, source_text)
            candidate = entry.get('values') if key == 'review_flags' else entry.get('value')
            if known and key == 'review_flags':
                if not isinstance(value, list) or not isinstance(candidate, list):
                    raise ValueError('invalid historical review flags')
                if any(not isinstance(flag, str) for flag in value + candidate):
                    raise ValueError('invalid historical review flags')
                if not set(value) <= set(candidate):
                    raise EvidenceMismatch('manifest drops a historical review flag')
            elif known and candidate != value:
                raise EvidenceMismatch('manifest and historical review status disagree')
            return True, candidate
    return known, value


_CURRENCY_NAMES = {'USD': 'USD', '美元': 'USD', 'HKD': 'HKD', '港元': 'HKD', '港币': 'HKD',
                   'CNY': 'CNY', '人民币': 'CNY', 'TWD': 'TWD', '新台币': 'TWD', 'EUR': 'EUR', '欧元': 'EUR'}


def _currency_evidence(kind, historical, source_text, manifest, output, warnings, differences):
    value, source, excerpt = None, 'unknown', None
    if kind == 'json':
        raw = historical.get('currency')
        if raw is None and isinstance(historical.get('units'), dict):
            raw = historical['units'].get('currency')
        if isinstance(raw, str):
            value = _CURRENCY_NAMES.get(raw.strip())
            if value is not None:
                source = 'structured_record'
    entry = manifest.get('currency')
    if entry is not None:
        if not isinstance(entry, dict) or entry.get('state') not in ('known', 'unknown'):
            raise ValueError('invalid currency evidence state')
        if entry['state'] == 'known':
            _cited(entry, source_text)
            attested = entry.get('value')
            if attested not in ('USD', 'HKD', 'CNY', 'TWD', 'EUR') or entry.get('scope') != 'valuation_band':
                warnings.add('currency_citation_unverified')
            elif value is not None and value != attested:
                _difference(differences, warnings, 'currency.source', value, attested, 'unit_difference')
            elif value is None:
                value, source, excerpt = attested, 'operator_attested', entry['source_excerpt']
                warnings.add('currency_attested')
    if value is None:
        warnings.add('currency_unknown')
    elif value != output['currency']:
        _difference(differences, warnings, 'currency', value, output['currency'], 'unit_difference')
    return {'state': 'known' if value is not None else 'unknown', 'value': value,
            'source': source, 'source_excerpt': excerpt}


def compare(result_bytes, kind, output, manifest_bytes=None):
    """Return frozen comparison metadata; raise for explicit conflicts or malformed citations."""
    from hashlib import sha256
    source_text = result_bytes.decode('utf-8')
    manifest = _manifest(manifest_bytes, output['ticker'], sha256(result_bytes).hexdigest())
    warnings, precision, differences = set(), {}, []
    if manifest:
        # A cited transcription is auditable, not an independent execution
        # proof or proof that the operator interpreted the text correctly.
        warnings.add('historical_material_unreviewed')
    inferred_places = {}
    unrounded_fields = set()
    historical = {}
    source_prefix = ''
    if kind == 'json':
        historical = _load_json(result_bytes)
        if isinstance(historical, dict) and 'unrounded_fields' in historical:
            declared = historical['unrounded_fields']
            if not isinstance(declared, list) or any(not isinstance(field, str) for field in declared):
                raise ValueError('unrounded fields must be a list of field names')
            unrounded_fields = set(declared)
        if isinstance(historical, dict) and 'output' in historical:
            historical = historical['output']
            source_prefix = '/output'
        if not isinstance(historical, dict):
            raise ValueError('historical result must be an object')
        if not isinstance(historical.get('ticker'), str):
            warnings.add('historical_identity_unknown')
        elif historical['ticker'] != output['ticker']:
            _difference(differences, warnings, 'ticker', historical['ticker'], output['ticker'], 'identity_difference')
        historical_units = historical.get('units')
        if (isinstance(historical.get('currency'), str) and isinstance(historical_units, dict) and historical_units.get('currency') is not None
                and historical_units['currency'] != historical.get('currency')):
            _difference(differences, warnings, 'currency.metadata', historical_units['currency'],
                        output['currency'], 'unit_difference')
        old_anchors = _partial_band(historical.get('anchors'))
        candidate = _state(historical, 'candidate', _candidate, source_prefix)
        option = _state(historical, 'option', _option, source_prefix)
    elif kind == 'markdown':
        # Prose is retained solely as an exact citation source. Its format,
        # ticker, bands, currency and precision are never guessed by code.
        old_anchors = {}
        candidate = {'state': 'unknown', 'source': 'unreviewed_prose'}
        option = {'state': 'unknown', 'source': 'unreviewed_prose'}
        warnings.add('historical_identity_unknown')
        if not manifest:
            warnings.add('historical_material_unreviewed')
    else:
        raise ValueError('unsupported historical evidence format')

    base = {'state': 'known' if old_anchors else 'unknown',
            'source': 'structured_record' if old_anchors else 'unknown', 'data': dict(old_anchors)}
    base_entry = manifest.get('anchors')
    if base_entry is not None:
        if not isinstance(base_entry, dict) or base_entry.get('state') not in ('known', 'unknown'):
            raise ValueError('invalid historical base state')
        if base_entry['state'] == 'known':
            _cited(base_entry, source_text)
            provided = _partial_band(base_entry.get('data'))
            if not provided:
                raise ValueError('historical base manifest has no numeric tiers')
            for tier, value in provided.items():
                if tier in old_anchors and old_anchors[tier] != value:
                    _difference(differences, warnings, 'anchors.source.' + tier, old_anchors[tier], value, 'source_conflict')
                elif tier not in old_anchors:
                    old_anchors[tier] = value
            base = {'state': 'known', 'source': 'operator_attested', 'data': dict(old_anchors),
                    'source_excerpt': base_entry['source_excerpt'], 'rationale': base_entry['rationale']}
            warnings.add('historical_transcription_attested')
    currency = _currency_evidence(kind, historical, source_text, manifest, output, warnings, differences)
    candidate = _merge_part(candidate, manifest.get('candidate'), 'candidate', source_text)
    option = _merge_part(option, manifest.get('option'), 'option', source_text)
    if candidate.get('source') == 'operator_attested' or option.get('source') == 'operator_attested':
        warnings.add('historical_material_unreviewed')
    for tier in ('L', 'M', 'H'):
        if tier in old_anchors:
            _compare_number(old_anchors[tier], output['anchors'][tier], f'anchors.{tier}', manifest, source_text,
                            inferred_places, warnings, precision, differences, unrounded_fields)
        else:
            warnings.add('anchors_unknown')
    for part, state in (('candidate', candidate), ('option', option)):
        actual = output.get(part)
        if state['state'] == 'unknown':
            warnings.add(part + '_unknown')
            continue
        if state['state'] == 'absent':
            if actual is not None:
                _difference(differences, warnings, f'{part}.presence', 'absent', 'present', 'band_presence_difference')
            if state.get('source') == 'operator_attested':
                warnings.add(part + '_absence_attested')
            continue
        old = state['data']
        if part == 'candidate' and 'label' not in old:
            warnings.add('candidate_label_unknown')
        if old.get('status') != 'unavailable' and len(old.get('anchors', {})) < 3:
            warnings.add(part + '_anchors_unknown')
        if old.get('status') == 'unavailable' and old.get('anchors'):
            warnings.add('historical_' + part + '_internally_inconsistent')
            _difference(differences, warnings, part + '.source_consistency',
                        {'status': 'unavailable', 'anchors': old['anchors']},
                        actual.get('status') if isinstance(actual, dict) else None,
                        'historical_internal_conflict')
        if actual is None:
            _difference(differences, warnings, f'{part}.presence', 'present', 'absent', 'band_presence_difference')
            continue
        if old.get('status') == 'unavailable' or actual.get('status') == 'unavailable':
            if old.get('status') != actual.get('status'):
                _difference(differences, warnings, part + '.availability',
                            old.get('status') or 'available', actual.get('status') or 'available',
                            'band_availability_difference')
            if part == 'candidate' and old.get('label') is not None and old.get('label') != actual.get('label'):
                _difference(differences, warnings, 'candidate.label', old['label'], actual.get('label'), 'label_difference')
            continue
        if part == 'candidate':
            if 'label' in old and old['label'] != actual.get('label'):
                _difference(differences, warnings, 'candidate.label', old['label'], actual.get('label'), 'label_difference')
        if 'anchors' in old:
            actual_band = _band(actual.get('anchors'))
            for tier in ('L', 'M', 'H'):
                if tier in old['anchors']:
                    _compare_number(old['anchors'][tier], actual_band[tier], f'{part}.anchors.{tier}', manifest,
                                    source_text, inferred_places, warnings, precision, differences, unrounded_fields)
                else:
                    warnings.add(part + '_anchors_unknown')
        else:
            warnings.add(part + '_anchors_unknown')
        if part == 'option':
            if 'value' in old:
                if actual.get('value') is None:
                    _difference(differences, warnings, 'option.value', old['value'], None, 'missing_current_value')
                else:
                    _compare_number(old['value'], actual['value'], 'option.value', manifest, source_text,
                                    inferred_places, warnings, precision, differences, unrounded_fields)
            elif actual.get('value') is not None:
                warnings.add('option_value_unknown')
            if 'value_unit' in old and actual.get('value_unit') != old['value_unit']:
                _difference(differences, warnings, 'option.value_unit', old['value_unit'],
                            actual.get('value_unit'), 'unit_difference')
            if 'value_unit' not in old and actual.get('value') is not None:
                warnings.add('option_value_unit_unknown')
            elif 'value_unit' in old and _unit_declarations(output, 'option') is None:
                warnings.add('option_value_unit_unknown')
            elif 'value_unit' in old and old['value_unit'] != _unit_declarations(output, 'option'):
                warnings.add('option_value_unit_incompatible')

    manifest_unit = manifest.get('price_unit')
    attested_unit = None
    if manifest_unit is not None and manifest_unit.get('state') == 'known':
        _cited(manifest_unit, source_text)
        attested_unit = manifest_unit.get('value')
        if not isinstance(attested_unit, str) or not attested_unit.strip():
            raise ValueError('invalid attested price unit')
        warnings.add('price_unit_attested')
    elif manifest_unit is not None and manifest_unit.get('state') != 'unknown':
        raise ValueError('invalid price unit state')
    price_units = {}
    for part in ('anchors', 'candidate', 'option'):
        state = candidate if part == 'candidate' else option if part == 'option' else None
        if part != 'anchors' and output.get(part) is None and state['state'] != 'present':
            continue
        old_unit = _unit_declarations(historical, part, tolerate_incomplete=True)
        old_unit_source = 'structured_record' if old_unit is not None else 'unknown'
        part_unit = state.get('price_unit') if isinstance(state, dict) else None
        if part_unit is not None and old_unit is not None and part_unit != old_unit:
            _difference(differences, warnings, part + '.unit_source', old_unit, part_unit, 'unit_difference')
        if old_unit is None and part_unit is not None:
            old_unit_source = 'operator_attested_band'
        old_unit = old_unit or part_unit
        new_unit = _unit_declarations(output, part)
        if attested_unit is not None and old_unit is not None and attested_unit != old_unit:
            _difference(differences, warnings, part + '.unit_source', old_unit, attested_unit, 'unit_difference')
        if old_unit is None and attested_unit is not None:
            old_unit_source = 'operator_attested_price_unit'
        old_unit = old_unit or attested_unit
        price_units[part] = {'historical': old_unit, 'current': new_unit, 'source': old_unit_source,
                             'state': 'known' if old_unit is not None else 'unknown'}
        if old_unit_source == 'operator_attested_band':
            price_units[part]['source_excerpt'] = state['unit_source_excerpt']
        elif old_unit_source == 'operator_attested_price_unit':
            price_units[part]['source_excerpt'] = manifest_unit['source_excerpt']
            price_units[part]['rationale'] = manifest_unit['rationale']
        if old_unit is not None and new_unit is not None and old_unit != new_unit:
            _difference(differences, warnings, part + '.unit', old_unit, new_unit, 'unit_difference')
        if old_unit is None or (output.get(part) is not None and new_unit is None):
            warnings.add(part + '_unit_unknown')

    known_flags, old_flags = _known_or_manifest(historical, 'review_flags', manifest.get('review_flags'), source_text)
    if known_flags:
        if not isinstance(old_flags, list) or any(not isinstance(flag, str) or not flag.strip() for flag in old_flags):
            raise ValueError('invalid historical review flags')
        if not set(old_flags) <= set(output.get('review_flags', [])):
            _difference(differences, warnings, 'review_flags', old_flags, output.get('review_flags', []), 'review_flags_difference')
    else:
        warnings.add('review_flags_unknown')
    old_option = historical.get('option') if isinstance(historical.get('option'), dict) else {}
    known_status, old_status = _known_or_manifest(old_option, 'status', manifest.get('option_status'), source_text)
    if known_status and (not isinstance(old_status, str) or not old_status.strip()):
        raise ValueError('invalid historical option status')
    if known_status and output.get('option') is None:
        _difference(differences, warnings, 'option.status', old_status, None, 'status_difference')
    if output.get('option') is not None:
        if known_status and old_status != output['option'].get('status'):
            _difference(differences, warnings, 'option.status', old_status, output['option'].get('status'), 'status_difference')
        if not known_status:
            warnings.add('option_status_unknown')
    specs = manifest.get('precision', {})
    if not isinstance(specs, dict) or set(specs) - set(precision):
        raise ValueError('precision refers to an uncompared field')
    historical_fund_status = historical.get('fund_reference_status')
    current_fund_status = output.get('fund_reference_status')
    if historical_fund_status is not None:
        if not isinstance(historical_fund_status, str) or not historical_fund_status.strip():
            raise ValueError('invalid historical fund reference status')
        if historical_fund_status != current_fund_status:
            _difference(differences, warnings, 'fund_reference_status', historical_fund_status,
                        current_fund_status, 'status_difference')
    elif current_fund_status is not None:
        warnings.add('fund_reference_status_unknown')
    for difference in differences:
        group = difference['field'].split('.')[0]
        if group in price_units:
            difference['historical_unit'] = price_units[group]['historical']
            difference['current_unit'] = price_units[group]['current']
    flags_source = ('operator_attested' if isinstance(manifest.get('review_flags'), dict)
                    and manifest['review_flags'].get('state') == 'known'
                    else 'structured_record' if known_flags else 'unknown')
    status_source = ('operator_attested' if isinstance(manifest.get('option_status'), dict)
                     and manifest['option_status'].get('state') == 'known'
                     else 'structured_record' if known_status else 'unknown')
    flags_detail = {'state': 'known' if known_flags else 'unknown', 'source': flags_source,
                    'values': old_flags if known_flags else None}
    status_detail = {'state': 'known' if known_status else 'unknown', 'source': status_source,
                     'value': old_status if known_status else None}
    for detail, field in ((flags_detail, 'review_flags'), (status_detail, 'option_status')):
        if detail['source'] == 'operator_attested':
            detail['source_excerpt'] = manifest[field]['source_excerpt']
            detail['rationale'] = manifest[field]['rationale']
        elif detail['source'] == 'structured_record':
            detail['source_pointer'] = source_prefix + ('/review_flags' if field == 'review_flags' else '/option/status')
    if any(flag.endswith('_unknown') or flag == 'historical_identity_unknown' for flag in warnings):
        warnings.add('historical_material_unreviewed')
    return {'anchors': base, 'candidate': candidate, 'option': option, 'currency': currency, 'precision': precision, 'price_units': price_units,
            'review_flags_state': 'known' if known_flags else 'unknown',
            'option_status_state': 'known' if known_status else 'unknown',
            'review_flags': flags_detail, 'option_status': status_detail,
            'fund_reference_status': {'historical': historical_fund_status, 'current': current_fund_status},
            'differences': differences, 'uncertainties': sorted(warnings)}
