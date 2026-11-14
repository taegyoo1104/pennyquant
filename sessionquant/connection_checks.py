"""전체 연결의 읽기 검사. LLM 생성·주문·미공개 청구 잔액 조회를 하지 않는다."""
import os
import json
from datetime import datetime,timezone
import httpx
from .news_setup import check_news
from .session_config import load_session_config
from .models import utc


def active_collector(repo):
    names=('trade_control_state','account_monitor_status')
    if repo.backend=='mysql':
        rows=repo._execute('SELECT name,payload FROM pq_controls WHERE name IN (%s,%s)',names,fetch='all')
        values=[(name,json.loads(value) if isinstance(value,str) else value) for name,value in rows]
    else: values=[(name,repo.get_control(name,{})) for name in names]
    now=datetime.now(timezone.utc)
    for name,state in values:
        try:
            t=state.get('heartbeat') if name=='trade_control_state' else state.get('updated_at')
            active=state.get('armed') if name=='trade_control_state' else state.get('status')=='ok'
            if active and t and 0<=(now-utc(t)).total_seconds()<60: return True
        except (ValueError,TypeError,AttributeError): pass
    return False


async def check_connections(config_path,*,offline=False,symbol='AAPL'):
    from .database.repository import Repository
    config=load_session_config(config_path)
    news=await check_news(config_path,connect=not offline,symbol=symbol)
    result={'read_only':True,'llm_generation_calls':0,'order_writes':0,'llm_credit_balance_verified':False,
        'news':news,'mysql':{},'toss':{}}
    repo=None; busy=False
    try:
        if offline: result['mysql']={'status':'not_tested','backend':config.database_backend}
        else:
            repo=Repository(config.db_path,backend=config.database_backend)
            # SELECT 성공을 확인한다. 키·접속 비밀번호는 출력하지 않는다.
            repo.get_control('runtime_kind')
            busy=active_collector(repo)
            result['mysql']={'status':'ok','backend':repo.backend}
    except Exception: result['mysql']={'status':'error','message':'DB 서버·계정·비밀번호·권한을 확인하세요.'}
    finally:
        if repo: repo.close()
    if offline:
        result['toss']={'status':'not_tested','client_id_configured':bool(os.getenv('TOSS_CLIENT_ID')),
            'client_secret_configured':bool(os.getenv('TOSS_CLIENT_SECRET'))}
    elif result['mysql']['status']=='error':
        result['toss']={'status':'skipped_database_unavailable','message':'실행 중인 토스 수집기 유무를 확인할 수 없습니다. DB 연결을 복구한 뒤 검사하세요.'}
    elif busy:
        result['toss']={'status':'skipped_active_collector','message':'토스 수집·거래 엔진을 종료한 뒤 검사하세요. 새 토큰 발급으로 기존 인증을 끊지 않습니다.'}
    elif not os.getenv('TOSS_CLIENT_ID') or not os.getenv('TOSS_CLIENT_SECRET'):
        result['toss']={'status':'missing_credentials'}
    else:
        from .collectors.toss_market import TossMarketClient
        from .account_monitor import fetch_account_snapshot
        client=TossMarketClient()
        try:
            await client.prices([symbol])
            snapshot=await fetch_account_snapshot(client)
            result['toss']={'status':'ok' if not snapshot['issues'] else 'partial','account_verified':True,
                'holdings_received':True,'issues':snapshot['issues'],'order_permissions_verified':False}
        except httpx.HTTPStatusError as exc:
            result['toss']={'status':'error','http_status':exc.response.status_code}
        except Exception:
            result['toss']={'status':'error','message':'토스 키·계좌 선택·조회 권한을 확인하세요.'}
        finally: await client.aclose()
    return result
