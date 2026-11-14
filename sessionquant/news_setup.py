"""뉴스 연결 점검. 주문·유료 텍스트 생성 없이 키 설정과 읽기 응답만 확인한다."""
import os
from urllib.parse import quote
import httpx
from .models import symbol_valid
from .macro.models import LLMSettings
from .macro.sources import OFFICIAL_FEEDS,FREDClient,FREDRequestError,GDELTClient,OfficialNewsClient,TreasuryClient
from .session_config import load_session_config


def aggregate_status(rows):
    """검사를 수행했다는 사실과 실제 수집 성공 여부를 구분한다."""
    good = sum(row.get('code') == 'ok' for row in rows)
    return 'ok' if rows and good == len(rows) else 'partial' if good else 'unavailable'


async def check_news(config_path,*,connect=False,provider='all',symbol='AAPL'):
    symbol_valid(symbol)
    config=load_session_config(config_path); llm=LLMSettings.from_dict(config.llm)
    report={'llm_generation_calls':0,'live_orders_enabled':False,'connect':connect,
        'news_enabled':config.news_enabled,'runtime_scope':'run(PAPER_LOCAL); live-local에는 뉴스 전략 미연결',
        'models':{'primary':llm.primary_model,'escalation':llm.escalation_model},
        'limits':{'daily_primary_calls':llm.daily_primary_call_cap,'daily_escalation_calls':llm.daily_escalation_call_cap,
            'monthly_soft_usd':str(llm.monthly_soft_budget_usd),'monthly_hard_usd':str(llm.monthly_hard_budget_usd)},
        'configured':{k:bool(os.getenv(k)) for k in ('OPENAI_API_KEY','FRED_API_KEY','APCA_API_KEY_ID','APCA_API_SECRET_KEY','SEC_USER_AGENT')},
        'official_feeds':{k:v[1] for k,v in OFFICIAL_FEEDS.items()},
        'company_rss_count':sum(len(c.get('rss_urls',[])) for c in config.companies.values()),'connections':{}}
    if not connect: return report
    providers=('openai','fred','alpaca','sec','rss','official','treasury','gdelt') if provider=='all' else (provider,)
    for name in providers:
        clients=[]
        try:
            if name=='openai':
                if not os.getenv('OPENAI_API_KEY'): raise ValueError('missing_key')
                client=httpx.AsyncClient(timeout=15,follow_redirects=False); clients.append(client)
                values=[]
                for model in dict.fromkeys((llm.primary_model,llm.escalation_model)):
                    response=await client.get('https://api.openai.com/v1/models/'+quote(model,safe=''),
                        headers={'Authorization':'Bearer '+os.environ['OPENAI_API_KEY']})
                    values.append({'model':model,'http_status':response.status_code,'accessible':response.is_success})
                result={'status':'checked','models':values,'structured_generation_verified':False}
            elif name=='fred':
                if not os.getenv('FRED_API_KEY'): raise ValueError('missing_key')
                client=FREDClient(); clients.append(client)
                rows=await client.fetch()
                result={'status':'ok','series':[r['series'] for r in rows],'auxiliary_only':True}
            elif name=='alpaca':
                if not os.getenv('APCA_API_KEY_ID') or not os.getenv('APCA_API_SECRET_KEY'): raise ValueError('missing_key')
                from .collectors.alpaca_news import AlpacaNewsClient
                client=AlpacaNewsClient(); clients.append(client)
                rows=await client.fetch([symbol])
                result={'status':'ok','events':len(rows),'real_time':False}
            elif name=='sec':
                if not os.getenv('SEC_USER_AGENT'): raise ValueError('missing_user_agent')
                from .collectors.sec_company import SecCompanyResolver
                from .collectors.sec_edgar import SecEdgarClient
                resolver=SecCompanyResolver(); client=SecEdgarClient(); clients.extend([resolver,client])
                company=await resolver.resolve(symbol)
                if company is None: raise ValueError('ticker_mapping_unavailable')
                rows=await client.submissions(company.cik)
                result={'status':'ok','symbol':symbol,'filings':len(rows)}
            elif name=='rss':
                from .collectors.rss_collector import RSSCollector
                client=RSSCollector(); clients.append(client); feeds=[]
                for ticker,company in config.companies.items():
                    for url in company.get('rss_urls',[]):
                        if len(feeds)>=10: break
                        try:
                            rows=await client.fetch(url,source='company_rss',symbols=(ticker,),verified=False)
                            feeds.append({'symbol':ticker,'status':'ok','events':len(rows)})
                        except (httpx.HTTPError,ValueError,RuntimeError):
                            feeds.append({'symbol':ticker,'status':'unavailable'})
                result={'status':'checked' if feeds else 'not_configured','feeds':feeds}
            elif name=='official':
                client=OfficialNewsClient(); clients.append(client)
                rows=await client.fetch(); result={'status':aggregate_status(client.status),'events':len(rows),'feeds':client.status}
            elif name=='treasury':
                client=TreasuryClient(); clients.append(client)
                rows=await client.fetch(); result={'status':'checked','events':len(rows),'publication_time':'unknown'}
            elif name=='gdelt':
                client=GDELTClient(topics=('us_monetary_policy',)); clients.append(client)
                rows=await client.fetch(); result={'status':aggregate_status(client.status),'events':len(rows),'topics':client.status,'fact_verification':False}
            else: raise ValueError('unknown_provider')
            report['connections'][name]=result
        except FREDRequestError as exc:
            report['connections'][name]={'status':'error','http_status':exc.http_status,
                'error_code':exc.code,'message':str(exc)}
        except httpx.HTTPStatusError as exc:
            # FRED URL에는 키가 있으므로 요청 URL·예외 원문을 출력하지 않는다.
            report['connections'][name]={'status':'error','http_status':exc.response.status_code}
        except (httpx.HTTPError,ValueError,RuntimeError,KeyError,TypeError) as exc:
            report['connections'][name]={'status':'unavailable','error_type':type(exc).__name__}
        finally:
            for client in clients: await client.aclose()
    return report
