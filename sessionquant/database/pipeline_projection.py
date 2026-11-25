"""Bounded, read-only system-page summary; nested selection evidence stays in DB."""
from .dashboard_projection import decode

FIELDS = (
    'id','kind','status','reason','code','data_status','preparing',
    'included_count','excluded_count','evaluated_count','final_candidate_count',
    'approved_count','approved','effective_enabled','block_entries','symbol',
    'observed_at','timestamp','evaluated_at','known_at','checked_at',
)
SCHEMA = dict.fromkeys((*FIELDS,'items_count'))


def expression(backend):
    pairs=[]
    for key in FIELDS:
        value=f"JSON_EXTRACT(payload,'$.{key}')"
        kind=f"JSON_TYPE({value})" if backend=='mysql' else f"JSON_TYPE(payload,'$.{key}')"
        text=f'CAST(JSON_QUOTE(LEFT(JSON_UNQUOTE({value}),512)) AS JSON)' if backend=='mysql' else f'SUBSTR({value},1,512)'
        scalar=f"CASE WHEN {kind} IN ('STRING','text') THEN {text} WHEN {kind} IN ('INTEGER','DOUBLE','BOOLEAN','NULL','integer','real','true','false','null') THEN {value} END"
        safe_kind=f"CASE WHEN {kind} IN ('OBJECT','ARRAY','object','array') THEN NULL ELSE {kind} END"
        pairs.append(f'JSON_ARRAY({safe_kind},{scalar})')
    items="JSON_EXTRACT(payload,'$.items')"
    kind=f'JSON_TYPE({items})' if backend=='mysql' else "JSON_TYPE(payload,'$.items')"
    length=f'JSON_LENGTH({items})' if backend=='mysql' else "JSON_ARRAY_LENGTH(payload,'$.items')"
    pairs.append(f"JSON_ARRAY(CASE WHEN {kind} IN ('ARRAY','array') THEN 'integer' END,{length})")
    return 'JSON_ARRAY('+','.join(pairs)+')'


def summary(raw):
    return decode(raw,SCHEMA)


def validate_limit(limit):
    if type(limit) is not int or not 1<=limit<=100:
        raise ValueError('처리 요약 조회는 1~100건입니다.')
