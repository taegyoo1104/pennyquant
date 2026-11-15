"""전송한 본문과 인용 위치를 결정론적으로 연결한다."""
from hashlib import sha256
import re


class ValidationFailure(ValueError):
    def __init__(self, code, field):
        self.code, self.field = code, field
        super().__init__(code+':'+field)


def digest(value):
    return sha256(value.encode('utf-8')).hexdigest()


def prepare_article(title, body, max_chars):
    title = ' '.join(title.split())
    body = re.sub(r'[\t\r ]+', ' ', body).strip()
    original_body=body
    # 추출기가 본문 앞에 남긴 동일 제목은 본문 인용 근거로 재사용하지 않는다.
    body='\n'.join(line for line in body.splitlines() if ' '.join(line.split())!=title).strip()
    allowance = max(0, max_chars-len(title)-1)
    sent = body[:allowance]
    if len(body) > allowance:
        # 긴 문장의 부정·조건절을 중간에서 자르지 않는다.
        boundaries = list(re.finditer(r'[.!?。](?:\s|$)|\n\n', sent))
        sent = sent[:boundaries[-1].end()].rstrip() if boundaries else ''
    paragraphs = []
    for match in re.finditer(r'[^\n]+', sent):
        text = match.group().strip()
        if not text: continue
        start = match.start()+len(match.group())-len(match.group().lstrip())
        paragraphs.append({'id': f'p{len(paragraphs)+1:04}', 'text': text, 'start': start,
                           'end': start+len(text), 'hash': digest(text)})
    return {'title': title, 'body': sent, 'text': title+'\n'+sent, 'paragraphs': paragraphs,
        'full_char_count': len(original_body), 'sent_char_count': len(sent), 'full_content_hash': digest(original_body),
        'sent_content_hash': digest(sent), 'truncated': len(sent)<len(body),
        'heading_removed':body!=original_body,'truncation_strategy': 'REMOVE_EXACT_HEADING_PREFIX_COMPLETE_SENTENCE_V1'}


def evidence_span(prepared, quote, status='UNKNOWN'):
    normalized = ' '.join(quote.split()) if isinstance(quote, str) else ''
    if not 8 <= len(normalized) <= 1000:
        raise ValidationFailure('EVIDENCE_QUOTE_MISMATCH', 'evidence')
    for paragraph in prepared['paragraphs']:
        text = paragraph['text']
        flat = ' '.join(text.split())
        at = flat.find(normalized)
        if at < 0: continue
        # 같은 문장의 생략된 부정/전망/조건부를 확정 인용으로 사용하지 않는다.
        sentences = re.split(r'(?<=[.!?。])\s+', flat)
        sentence = next((item for item in sentences if normalized in item), flat)
        if status == 'CONFIRMED' and re.search(
            r'\b(?:not|never|denied|rumou?r|unconfirmed|plans? to|may|might|could|would|if|'
            r'expects?|proposed|historically|last year|previously|in (?:19\d{2}|20[01]\d))\b', sentence, re.I):
            raise ValidationFailure('SEMANTIC_CONFLICT', 'evidence.status')
        return {'paragraph_id': paragraph['id'], 'paragraph_hash': paragraph['hash'],
            'start': at, 'end': at+len(normalized), 'offset_basis': 'NORMALIZED_SENT_PARAGRAPH',
            'quote_hash': digest(normalized)}
    raise ValidationFailure('EVIDENCE_QUOTE_MISMATCH', 'evidence')


def parse_completion(response):
    import json
    if len(response.content) > 100000:
        raise ValidationFailure('SCHEMA_MISMATCH', 'response_size')
    try:
        envelope = response.json()
        choice = envelope['choices'][0]
        message = choice['message']
    except (ValueError, KeyError, IndexError, TypeError):
        raise ValidationFailure('SCHEMA_MISMATCH', 'choices.message') from None
    if message.get('refusal'):
        raise ValidationFailure('PROVIDER_REFUSAL', 'message.refusal')
    if choice.get('finish_reason') != 'stop':
        raise ValidationFailure('INCOMPLETE_FINISH', 'finish_reason')
    try:
        return json.loads(message['content']), envelope.get('usage', {})
    except (ValueError, KeyError, TypeError):
        raise ValidationFailure('JSON_PARSE_ERROR', 'message.content') from None
