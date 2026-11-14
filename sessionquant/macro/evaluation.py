"""라벨이 있는 같은 기업 뉴스로 모델의 정확도·토큰·지연을 비교한다."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import json
import os
from pathlib import Path

from ..events import parse_event
from ..news.analysis import NewsAnalyzer
from .models import LLMSettings

async def evaluate_models(input_path, output, *, models=('gpt-4.1-mini','gpt-6-luna','gpt-6.1-terra'), execute=False):
    rows=[json.loads(line) for line in Path(input_path).read_text(encoding='utf-8').splitlines() if line.strip()]
    if not 1<=len(rows)<=20: raise ValueError('평가 자료는 1~20개로 제한합니다')
    for row in rows:
        event=parse_event({'kind':'news','data':row['data']}).data
        if row['expected_sentiment'] not in {'POSITIVE','NEGATIVE','NEUTRAL','MIXED','UNKNOWN'}:
            raise ValueError('expected_sentiment 라벨이 필요합니다')
        if row['ticker'] not in event.symbols: raise ValueError('기사와 평가 종목이 일치하지 않습니다')
    report={'execute':execute,'examples':len(rows),'models':list(models),'results':{},
            'prompt_language':'English','korean_prompt_comparison':'미실행: 언어 변경 전 별도 라벨 평가가 필요합니다'}
    if execute:
        if not os.getenv('OPENAI_API_KEY'): raise ValueError('본인 PC에 OPENAI_API_KEY를 설정하세요')
        from datetime import datetime,timezone
        now=datetime.now(timezone.utc)
        for model in models:
            settings=LLMSettings(primary_model=model)
            analyzer=NewsAnalyzer(model=model,max_calls=len(rows))
            outcomes=[]
            try:
                for row in rows:
                    original=parse_event({'kind':'news','data':row['data']}).data
                    # 오래된 평가 본문은 평가용 수신 시각으로만 재작성하며 거래 엔진에 넣지 않는다.
                    event=replace(original,published_at=now-timedelta(minutes=1),received_at=now)
                    result=await analyzer.analyze(event,row['ticker'],source_kind='rss',primary_verified=True,now=now)
                    cost=(Decimal(result.input_tokens)*settings.primary_input_per_million+
                          Decimal(result.output_tokens)*settings.primary_output_per_million)/Decimal(1000000)
                    outcomes.append({'expected':row['expected_sentiment'],'actual':result.overall_sentiment,
                        'correct':result.overall_sentiment==row['expected_sentiment'],'error':result.error,
                        'input_tokens':result.input_tokens,'output_tokens':result.output_tokens,
                        'latency_ms':result.latency_ms,'estimated_cost_usd':str(cost)})
            finally: await analyzer.aclose()
            report['results'][model]={'accuracy':sum(x['correct'] for x in outcomes)/len(outcomes),'cases':outcomes}
    Path(output).parent.mkdir(parents=True,exist_ok=True)
    Path(output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report
