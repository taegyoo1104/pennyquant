"""Conservative, explainable English event rules; not investment advice.

Rules inspect title and body together. Negative/dilution evidence overrides a
positive phrase; ambiguous, speculative, and unverified claims cannot authorize
an entry. SEC form names by themselves do not imply direction.
"""
from __future__ import annotations

import re
from ..models import Classification, NewsEvent


def _has(pattern: str, text: str) -> bool:
    return re.search(pattern, text, re.IGNORECASE) is not None


def classify(event: NewsEvent) -> Classification:
    text = " ".join((event.title, event.body)).lower()
    text = re.sub(r"\s+", " ", text)
    if not event.verified or not event.source.strip():
        return Classification("uncertain", "unverified", "출처 검증 안 됨", exclude=True)

    if _has(r"\b(rumou?rs?|unconfirmed|unsubstantiated|anonymous sources?|speculation)\b", text):
        return Classification("uncertain", "rumor", "소문·미확인 정보", exclude=True)

    dilution = _has(
        r"\b(public offering|registered direct offering|private placement|at.the.market offering|"
        r"equity offering|stock offering|share offering|dilut(?:ion|ive)|convertible (?:notes?|debt)|"
        r"issu(?:e|es|ed|ance|ing) (?:of )?(?:additional |new )?(?:common )?(?:shares|stock))\b", text
    )
    if dilution:
        return Classification("negative", "dilution", "주식 발행·희석 위험 문구 발견", exclude=True, dilution_risk=True)

    negative_patterns = [
        ("delisting", r"\b(delist(?:ing|ed)?|non.compliance|bankrupt(?:cy)?|insolven(?:t|cy))\b"),
        ("regulatory", r"\b(fda (?:rejects?|rejected|denies|denied)|complete response letter|clinical hold|warning letter|regulatory action)\b"),
        ("clinical_failure", r"\b((?:trial|study|clinical).{0,45}(?:fail(?:s|ed|ure)?|(?:did not|didn't|failed to) meet)|miss(?:ed|es) (?:its |the )?(?:primary |key )?endpoint)\b"),
        ("contract_loss", r"\b(contract (?:terminat(?:ed|ion)|cancel(?:led|ed|lation))|los(?:e|es|t) (?:a |the |major )?contract)\b"),
    ]
    for event_type, pattern in negative_patterns:
        if _has(pattern, text):
            return Classification("negative", event_type, "악재 문구 발견", exclude=True)

    # Check both word orders before matching catalysts: "not FDA approved"
    # and "FDA approval was not granted" carry the same uncertainty. These
    # broad local windows deliberately sacrifice recall in ambiguous articles.
    catalyst = r"(?:approv(?:al|ed|es?)|positive|successful|contract|partnership|endpoints?)"
    negation = r"(?:not|no|never|without|denied|reject(?:s|ed)?|fail(?:s|ed)(?: to)?)"
    if _has(rf"\b{negation}\b.{{0,65}}\b{catalyst}\b|\b{catalyst}\b.{{0,65}}\b{negation}\b", text):
        return Classification("uncertain", "negated_claim", "긍정 표현에 부정·제한 문구 포함", exclude=True)
    prospective = r"(?:seeks?|seeking|appl(?:y|ies|ied|ication)|await(?:s|ing)?|expects?|expected|anticipat(?:es?|ed)|potential|may|might|could|plans? to|subject to)"
    future_catalyst = r"(?:approv(?:al|ed)|contract|partnership|acqui(?:re|sition)|positive results)"
    if _has(rf"\b{prospective}\b.{{0,65}}\b{future_catalyst}\b|\b{future_catalyst}\b.{{0,65}}\b{prospective}\b", text):
        return Classification("uncertain", "prospective_claim", "예정·예상 이벤트는 확정 호재가 아님", exclude=True)

    positive_patterns = [
        ("fda_approval", r"\b(fda approv(?:es?|ed|al)|(?:receives?|received|secures?|secured|obtains?|obtained) (?:u\.?s\.? )?fda approval|approved by (?:the )?fda)\b"),
        ("clinical_positive", r"\b(positive (?:top.line |topline )?(?:phase [123i]+ |clinical |trial |study )?results|(?:met|meets|achieved) (?:its |the |all )?(?:primary |key )?endpoints?)\b"),
        ("contract", r"\b((?:wins?|won|awarded|secures?|secured|signs?|signed) (?:a |an |new |major |multi.year |\$[\d.,]+ (?:million |billion )?){0,4}contract|(?:announces?|announced) (?:a |new |major |strategic ){0,3}partnership)\b"),
        ("earnings", r"\b((?:earnings|revenue|sales) (?:beat|beats|exceed(?:s|ed)?) (?:estimates|expectations|consensus)|better.than.expected (?:earnings|revenue|results))\b"),
    ]
    for event_type, pattern in positive_patterns:
        if _has(pattern, text):
            return Classification("positive", event_type, "확정 이벤트 후보; 시장 반응 추가 검증 필요")

    if _has(r"\b(acquisition|merger|acquires?|takeover|buyout)\b", text):
        return Classification("uncertain", "merger", "인수 조건·대가·확정 여부 개별 분석 필요", exclude=True)
    if _has(r"\b(surges?|soars?|rall(?:y|ies)|stock rises|shares rise|stock gains|price jumps)\b", text):
        return Classification("neutral", "price_only", "단순 주가 상승 기사는 매수 근거에서 제외", exclude=True)
    return Classification("neutral", "unclassified", "호재·악재를 확정할 내용 부족")
