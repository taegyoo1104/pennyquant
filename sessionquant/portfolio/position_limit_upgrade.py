"""Explicit maintenance-only upgrade of an idle, flat strategy-lab ledger."""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path

from .broker import PaperBroker
from .models import PortfolioSettings, serialize

UPGRADE_ID = 'paper-position-limit-three-v1'
PROFILES = {'sessionquant.strategy-lab.example.yaml', 'sessionquant.strategy-lab-websocket.example.yaml'}


def upgrade(repo, settings):
    if settings.preferred_positions != 3 or settings.max_positions != 3:
        raise ValueError('이 전환은 보유 한도 3종목 설정에만 적용합니다.')
    with repo.engine_lease(), repo.transaction():
        state = repo.load_state()
        if state is None:
            return False
        if (state.get('environment') != 'PAPER_LOCAL' or state.get('strategy') != 'pullback_simple'
                or state.get('synthetic') is not False or state.get('paper_data_policy') != 'observed_paper_v1'):
            raise ValueError('현재 실데이터 모의 실험 장부만 전환할 수 있습니다.')
        stored = state.get('broker')
        if not isinstance(stored, dict) or any(not isinstance(stored.get(key), dict)
                for key in ('positions', 'orders', 'exit_intents')):
            raise ValueError('보유·주문 장부 형식을 확인하지 못했습니다.')
        broker = PaperBroker.from_state(state['broker'])
        if broker.settings == settings:
            return False
        previous = replace(settings, preferred_positions=2, max_positions=5)
        if broker.settings != previous:
            raise ValueError('보유 수 외의 위험·비용 설정도 달라 자동 변경하지 않습니다.')
        control = repo.get_control('trade_control_state', {}) or {}
        if (control.get('mode') != 'paper' or control.get('status') != 'OFFLINE'
                or control.get('armed') is not False or control.get('running') is not False):
            raise ValueError('프로그램을 정상 종료한 뒤 보유 한도를 전환하세요.')
        if broker.positions or broker.get_open_orders() or broker.exit_intents:
            raise ValueError('보유·미체결·청산 요청이 남아 있습니다. 정리 완료 후 업데이트하세요.')
        if repo.backend == 'mysql':
            # Count the entire ledger without loading a large order journal.
            pending = repo._execute("SELECT COUNT(*) FROM pq_order_intents WHERE namespace=%s "
                "AND JSON_UNQUOTE(JSON_EXTRACT(payload,'$.kind'))='dashboard_command' "
                "AND JSON_UNQUOTE(JSON_EXTRACT(payload,'$.status'))='pending'",
                (repo.namespace,), fetch='one')[0]
        else:
            pending = any(r.get('kind') == 'dashboard_command' and r.get('status') == 'pending'
                          for r in repo.all('order_intents'))
        if pending:
            raise ValueError('처리 중인 화면 요청이 있어 보유 한도를 변경하지 않습니다.')
        updated = deepcopy(state)
        updated['broker']['settings'] = serialize(settings)
        repo.save_state(updated)
        repo.record('schema_migrations', {'id': UPGRADE_ID, 'timestamp': datetime.now(timezone.utc),
            'previous_settings': serialize(previous), 'settings': serialize(settings),
            'reason': '사용자 요청: 동시 보유·미체결 매수 최대 3종목'})
        return True


def main():
    from ..database.repository import Repository
    from ..session_config import load_session_config
    path = Path(os.environ.get('SQ_SESSION_CONFIG') or 'config/sessionquant.realtime-paper.example.yaml')
    if path.name not in PROFILES:
        print('기존 프로필: 보유 한도 변경 대상이 아닙니다.')
        return
    config = load_session_config(path)
    if config.environment != 'PAPER_LOCAL' or config.strategy != 'pullback_simple':
        raise ValueError('모의 실험 프로필만 전환할 수 있습니다.')
    os.environ['SQ_ENVIRONMENT'] = config.environment
    with closing(Repository(config.db_path, backend=config.database_backend, initialize=False)) as repo:
        changed = upgrade(repo, PortfolioSettings(**config.portfolio))
    print('보유 한도 3종목 적용 완료. 잔액·거래 기록은 보존했습니다.' if changed else '보유 한도 전환 불필요: 새 장부 또는 이미 적용된 설정입니다.')


if __name__ == '__main__':
    try:
        main()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    except Exception:
        raise SystemExit('보유 한도 전환 실패. DB 연결·종료 상태를 확인하세요. 프로그램을 시작하지 않습니다.') from None
