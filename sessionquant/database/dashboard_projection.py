"""Read only dashboard fields from recovery JSON; never truncate financial strings."""
import json


ACCOUNT = {key: None for key in (
    'cash', 'equity', 'reserved_cash', 'realized_pnl', 'day_start_equity', 'positions',
    'session_date', 'exit_intents')}
ACCOUNT['settings'] = dict.fromkeys(('initial_cash', 'max_positions', 'preferred_positions'))
STATE = {
    **dict.fromkeys(('environment', 'synthetic', 'strategy', 'last_time', 'candidate_selection')),
    **ACCOUNT,
    'account': ACCOUNT,
    'broker': {**ACCOUNT, 'account': ACCOUNT},
    'portfolio': {**ACCOUNT, 'account': ACCOUNT},
}
ORDER = {**dict.fromkeys(('client_id', 'symbol', 'side', 'reason')),
         'intent': dict.fromkeys(('client_id', 'symbol', 'side', 'reason'))}


def fields(schema, prefix=()):
    for name, children in schema.items():
        path = (*prefix, name)
        yield path, children is not None
        if children is not None:
            yield from fields(children, path)


def expression(schema, backend):
    """Type tags distinguish a missing key from an explicit JSON null/false."""
    rows = []
    for path, container in fields(schema):
        # Paths come only from the fixed schemas above, never user input.
        target = '$.' + '.'.join(path)
        value = f"JSON_EXTRACT(payload,'{target}')"
        kind = f"JSON_TYPE({value})" if backend == 'mysql' else f"JSON_TYPE(payload,'{target}')"
        rows.append(f'JSON_ARRAY({kind}' + (')' if container else f',{value})'))
    return 'JSON_ARRAY(' + ','.join(rows) + ')'


def decode(raw, schema):
    rows = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    result = {}
    for (path, container), row in zip(fields(schema), rows, strict=True):
        kind = row[0]
        if kind is None:
            continue
        kind = kind.lower()
        parent = result
        for name in path[:-1]:
            parent = parent[name]
        if container:
            if kind not in {'object', 'null'}:
                raise ValueError('대시보드 계좌 기록 형식을 확인하세요.')
            parent[path[-1]] = {} if kind == 'object' else None
        else:
            parent[path[-1]] = kind == 'true' if kind in {'true', 'false'} else row[1]
    return result
