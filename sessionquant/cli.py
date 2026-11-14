"""한국어 명령행. 소수점 실주문 검증은 별도 live-local에서 명시적으로 활성화한다."""
from __future__ import annotations

import argparse
import asyncio
from importlib.metadata import version
import json
from pathlib import Path
import sys
import httpx

from .legacy_cli import metadata


def parser():
    root = argparse.ArgumentParser(prog="sessionquant", description="S&P 500 장중 전략·소수점 모의매매·MySQL·뉴스 위험 분석")
    sub = root.add_subparsers(dest="command", required=True)
    live=sub.add_parser("live-local", help="Phase 5 소액 검증; 기본은 주문 없는 계좌 검사")
    live.add_argument("--config",type=Path,default=Path("config/live.local.yaml"))
    live.add_argument("--execute",action="store_true",help="실제 시장가 매수·매도 검증 활성화")
    live.add_argument("--confirm-account-seq",type=int)
    live.add_argument("--acknowledge-live-orders",action="store_true")
    live.add_argument("--max-polls",type=int)
    live.add_argument("--dashboard",action="store_true",help="대시보드를 열고 매매 시작 버튼 대기")
    live.add_argument("--port",type=int,default=8502)
    account=sub.add_parser("run-account",help="내 실제 계좌 보유·매수 가능 금액·미체결 조회; 주문 없음")
    account.add_argument("--db",type=Path,default=Path("data/sessionquant-account.db"))
    account.add_argument("--interval",type=int,default=10)
    account.add_argument("--port",type=int,default=8502)
    account.add_argument("--no-dashboard",action="store_true")
    account.add_argument("--max-polls",type=int)
    all_check=sub.add_parser("check-connections",help="토스·DB·OpenAI·뉴스 전체 연결 점검; 주문·LLM 생성 없음")
    all_check.add_argument("--config",type=Path,default=Path("config/sessionquant.yaml"))
    all_check.add_argument("--offline",action="store_true",help="네트워크·DB에 연결하지 않고 설정만 점검")
    all_check.add_argument("--symbol",default="AAPL")
    news_setup=sub.add_parser("setup-news",help="OpenAI·FRED·Alpaca·SEC 접속 설정을 한 번 저장")
    news_setup.add_argument("--from-current",action="store_true")
    news_check=sub.add_parser("check-news",help="뉴스 키·소스·모델 설정 점검; 기본은 연결하지 않음")
    news_check.add_argument("--config",type=Path,default=Path("config/sessionquant.yaml"))
    news_check.add_argument("--connect",action="store_true",help="주문·LLM 생성 없이 API 읽기 연결 검사")
    news_check.add_argument("--provider",choices=("all","openai","fred","alpaca","sec","rss","official","treasury","gdelt"),default="all")
    news_check.add_argument("--symbol",default="AAPL")
    setup=sub.add_parser("setup-env",help="한 번 저장한 로컬 접속 설정을 다음 실행에서 자동 사용")
    setup.add_argument("--from-current",action="store_true",help="현재 환경변수를 비밀값 출력 없이 .env에 저장")
    for name, desc in [('watchlist-status','시총 감시·집중 목록 상태 조회'),
                       ('watchlist-refresh','검증 시총 파일로 감시목록 갱신; 외부 API·주문 없음'),
                       ('watchlist-export','수집 당시 감시·집중 목록 이력 내보내기')]:
        cmd = sub.add_parser(name,help=desc)
        cmd.add_argument('--config',type=Path,default=Path('config/sessionquant.yaml'))
        if name == 'watchlist-export':
            cmd.add_argument('--output',type=Path,required=True)
    sub.add_parser("doctor", help="설치 상태와 실제 주문 비활성화 여부 확인")
    run = sub.add_parser("run", help="로컬 MySQL과 토스 읽기 전용 시세로 모의 운영")
    run.add_argument("--config", type=Path, default=Path("config/sessionquant.yaml"), help="운영 설정 YAML")
    run.add_argument("--no-dashboard", action="store_true", help="대시보드 없이 실행")
    run.add_argument("--port", type=int, default=8501, help="로컬 대시보드 포트")
    run.add_argument("--max-cycles", type=int, help="지정한 시세 수집 횟수 후 종료")
    run.add_argument("--no-desktop-notifications", action="store_true", help="바탕화면 알림 끄기")
    demo = sub.add_parser("demo", help="외부 API 없이 합성 자료로 전략·주문·청산 검증")
    demo.add_argument("--out", type=Path, default=Path("data/session-demo"), help="새 출력 폴더")
    demo.add_argument("--strategy", choices=("momentum", "pullback", "gap_breakout", "vwap"), default="pullback")
    demo.add_argument("--backend", choices=("sqlite", "mysql"), default="sqlite", help="합성 검증 자료의 저장소")
    replay = sub.add_parser("replay", help="수신 시각을 보존한 자료로 모의 주문 재생")
    backtest = sub.add_parser("backtest", help="동일 자료·비용으로 전략 및 추가매수 효과 비교")
    for cmd in (replay, backtest):
        cmd.add_argument("--input", required=True, type=Path, help="정규화된 JSONL 자료")
        cmd.add_argument("--universe", required=True, type=Path, help="시점별 S&P 500 구성 이력")
        cmd.add_argument("--config", type=Path, help="위험·수수료 설정 YAML")
    replay.add_argument("--db", type=Path, default=Path("data/sessionquant-replay.db"))
    replay.add_argument("--backend", choices=("sqlite", "mysql"), default="sqlite")
    replay.add_argument("--strategy", choices=("momentum", "pullback", "gap_breakout", "vwap"), default="pullback")
    replay.add_argument("--watchlist-history",type=Path,help="당시 저장된 감시·집중 목록 이력 JSON")
    replay.add_argument("--resume", action="store_true", help="동일 입력과 설정으로 중단 지점 복구")
    backtest.add_argument("--report", type=Path, default=Path("data/session-report.json"))
    universe = sub.add_parser("universe-refresh", help="현재 구성 목록을 오늘부터 유효한 관측으로 저장")
    universe.add_argument("--output", type=Path, default=Path("data/sp500-universe.json"))
    for command, help_text in (("db-check", "DB 연결·스키마 확인"), ("db-stats", "실제 저장 행 수·용량 확인"),
                               ("db-prune", "시세 보관 기간 정리 미리보기"), ("db-migrate", "기존 SQLite를 빈 MySQL 실행 공간으로 복사")):
        db = sub.add_parser(command, help=help_text)
        db.add_argument("--db", type=Path, default=Path("data/sessionquant-paper-local.db"))
        db.add_argument("--backend", choices=("mysql", "sqlite"), default="mysql")
        if command == "db-migrate":
            db.add_argument("--source", required=True, type=Path, help="보존할 기존 SQLite 파일")
        if command == "db-prune":
            db.add_argument("--days", type=int, default=7)
            db.add_argument("--kinds", default="price_samples,quote_snapshots", help="정리할 관측 자료 종류")
            db.add_argument("--apply", action="store_true", help="미리보기 후 실제 삭제")
    dashboard = sub.add_parser("dashboard", help="저장된 모의 운영 결과를 한국어 화면으로 열기")
    dashboard.add_argument("--db", type=Path, default=Path("data/sessionquant-paper-local.db"))
    dashboard.add_argument("--port", type=int, default=8501)
    dashboard.add_argument("--backend", choices=("sqlite", "mysql"), default="mysql")
    evaluation = sub.add_parser("llm-evaluate", help="동일 라벨 자료로 모델 비교; 기본은 호출 없는 미리보기")
    evaluation.add_argument("--input", type=Path, required=True)
    evaluation.add_argument("--output", type=Path, default=Path("data/model-evaluation.json"))
    evaluation.add_argument("--execute", action="store_true", help="실제 API 호출과 토큰 과금 허용")
    calendar = sub.add_parser("calendar-import", help="확인된 공식 경제 발표 일정 가져오기")
    calendar.add_argument("--input", type=Path, required=True)
    calendar.add_argument("--db", type=Path, default=Path("data/sessionquant-paper-local.db"))
    calendar.add_argument("--backend", choices=("mysql", "sqlite"), default="mysql")
    sub.add_parser("check-toss", help="토스 인증·시세 읽기 전용 연결 검사")
    sub.add_parser("collect", help="기존 기업 뉴스·시세 수집 명령; collect --help로 옵션 확인")
    sub.add_parser("legacy", help="기존 PennyQuant 기록·분석 명령; 실제 주문은 차단")
    return root


def main(argv=None):
    from .local_env import load_local_env
    try:
        load_local_env()
    except (ImportError,OSError,ValueError):
        print("로컬 .env를 읽지 못했습니다. requirements.lock 설치와 파일 접근 권한을 확인하세요.",file=sys.stderr)
        return 2
    argv = list(sys.argv[1:] if argv is None else argv)
    if (not argv or argv[0]!="live-local") and any(x in {"--live", "--acknowledge-live-orders"} for x in argv):
        print("SessionQuant Phase 0~4에서는 실제 주문을 실행할 수 없습니다.", file=sys.stderr)
        return 2
    # 기존 자료 재생과 수집 명령은 식별자·장부 형식을 바꾸지 않고 유지한다.
    if argv and argv[0] in {"legacy", "check-toss", "collect", "run-toss", "run-system", "demo-system", "paper"}:
        from .legacy_cli import main as legacy_main
        return legacy_main(argv[1:] if argv[0] == "legacy" else argv)
    args = parser().parse_args(argv)
    try:
        import os
        os.environ.setdefault("SQ_ENVIRONMENT", "PAPER_LOCAL")
        if args.command in {'watchlist-status','watchlist-refresh','watchlist-export'}:
            from .session_config import load_session_config
            from .watchlist.commands import command
            report = asyncio.run(command(load_session_config(args.config),args.command,output=getattr(args,'output',None)))
            print(json.dumps(report,ensure_ascii=False,indent=2))
        elif args.command == "check-connections":
            from .connection_checks import check_connections
            print(json.dumps(asyncio.run(check_connections(args.config,offline=args.offline,symbol=args.symbol)),ensure_ascii=False,indent=2))
        elif args.command == "setup-news":
            from .local_env import setup_local_env
            setup_local_env(from_current=args.from_current,news_only=True)
        elif args.command == "check-news":
            from .news_setup import check_news
            print(json.dumps(asyncio.run(check_news(args.config,connect=args.connect,provider=args.provider,symbol=args.symbol)),ensure_ascii=False,indent=2))
        elif args.command == "run-account":
            from .account_monitor import run_account_monitor
            return asyncio.run(run_account_monitor(db_path=args.db,interval=args.interval,port=args.port,
                no_dashboard=args.no_dashboard,max_polls=args.max_polls))
        elif args.command == "setup-env":
            from .local_env import setup_local_env
            setup_local_env(from_current=args.from_current)
        elif args.command == "live-local":
            from .live.runtime import live_local
            result=asyncio.run(live_local(args.config,execute=args.execute,confirm_account_seq=args.confirm_account_seq,
                acknowledge=args.acknowledge_live_orders,max_polls=args.max_polls,dashboard=args.dashboard,port=args.port))
            print(json.dumps(result,ensure_ascii=False,indent=2))
        elif args.command == "doctor":
            print(json.dumps({"project": "SessionQuant", "version": "2.1.0", "python": sys.version.split()[0],
                "environment": "PAPER_LOCAL", "live_orders_enabled": False,
                "packages": {p: version(p) for p in ("httpx", "streamlit", "exchange-calendars", "PyMySQL")}},
                ensure_ascii=False, indent=2))
        elif args.command == "llm-evaluate":
            from .macro.evaluation import evaluate_models
            print(json.dumps(asyncio.run(evaluate_models(args.input,args.output,execute=args.execute)),ensure_ascii=False,indent=2))
        elif args.command == "calendar-import":
            from .macro.models import EconomicEvent
            from .macro.sources import official_url
            from .database.repository import Repository
            from .models import utc
            rows=json.loads(args.input.read_text(encoding="utf-8"))
            events=[EconomicEvent(**{**row,"scheduled_at":utc(row["scheduled_at"]),"known_at":utc(row["known_at"])}) for row in rows]
            if any(not official_url(event.source_url) or event.verified is not True for event in events):
                raise ValueError("확인한 공식 경제 일정이 필요합니다")
            from dataclasses import asdict
            repo=Repository(args.db,backend=args.backend)
            try:
                with repo.engine_lease(),repo.transaction():
                    for event in events: repo.record("economic_events",asdict(event))
            finally: repo.close()
            print(f"공식 발표 일정 {len(events)}개를 저장했습니다.")
        elif args.command == "run":
            from .session_config import load_session_config
            from .session_runtime import run_session
            return asyncio.run(run_session(load_session_config(args.config), no_dashboard=args.no_dashboard,
                port=args.port, max_cycles=args.max_cycles, desktop_notifications=not args.no_desktop_notifications))
        elif args.command == "universe-refresh":
            from .market.universe import UniverseStore
            result = asyncio.run(UniverseStore(args.output).refresh_public())
            print(json.dumps(result, ensure_ascii=False, indent=2))
            print(f"현재 목록을 저장했습니다: {args.output}. 과거 백테스트의 구성 이력으로 소급하지 않습니다.")
        elif args.command == "demo":
            from .session_demo import write_session_demo
            result = write_session_demo(args.out, strategy=args.strategy, backend=args.backend)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            print("합성 검증을 완료했습니다. 실제 시세·투자 성과·실제 주문이 아닙니다.")
        elif args.command in {"replay", "backtest"}:
            from .portfolio.models import PortfolioSettings
            from .session_config import load_session_config
            from .session_runtime import replay_session
            settings = PortfolioSettings(**load_session_config(args.config).portfolio) if args.config else PortfolioSettings()
            synthetic = metadata(args.input).get("synthetic", False) is True
            if args.command == "replay":
                result = replay_session(args.input, args.db, args.universe, strategy=args.strategy, resume=args.resume,
                    synthetic=synthetic, backend=args.backend, settings=settings, watchlist_history=args.watchlist_history)
                print(json.dumps({"db": str(args.db), "equity": result["state"]["equity"], "fills": len(result["fills"])}, ensure_ascii=False))
            else:
                from .session_backtest import compare_session
                compare_session(args.input, args.universe, args.report, settings=settings, synthetic=synthetic)
                print(f"성과와 비용 비교 보고서: {args.report} / {args.report.with_suffix('.md')}")
        elif args.command.startswith("db-"):
            from .database.operations import check_database, migrate_sqlite, prune_database
            from .database.repository import Repository
            os.environ["SQ_DB_BACKEND"] = args.backend
            if args.command == "db-migrate":
                result = migrate_sqlite(args.source, args.db)
            elif args.command == "db-prune":
                result = prune_database(args.db, args.days, tuple(x.strip() for x in args.kinds.split(",")), apply=args.apply)
            elif args.command == "db-check":
                result = check_database(args.db)
            else:
                repo = Repository(args.db)
                try:
                    result = repo.storage_stats()
                finally:
                    repo.close()
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        elif args.command == "dashboard":
            import subprocess
            from .notifications import dashboard_environment
            if not 1024 <= args.port <= 65535:
                raise ValueError("대시보드 포트는 1024~65535여야 합니다.")
            os.environ["SQ_DB_BACKEND"] = args.backend
            app = Path(__file__).parent / "dashboard" / "session_app.py"
            return subprocess.call([sys.executable, "-m", "streamlit", "run", str(app),
                "--server.address", "127.0.0.1", "--server.port", str(args.port), "--", "--db", str(args.db)],
                env=dashboard_environment(database=True))
        return 0
    except httpx.HTTPError as exc:
        status=getattr(getattr(exc,"response",None),"status_code",None)
        print(f"SessionQuant: 토스/API 연결 또는 응답 실패 (HTTP {status or '미확인'}). 실제 주문·잔량은 앱에서 확인하세요.",file=sys.stderr)
        return 2
    except (ValueError, RuntimeError, OSError, ImportError, KeyError, TypeError) as exc:
        print(f"SessionQuant: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        message="실거래 검증 중지: 주문 취소·청산 완료를 의미하지 않습니다. 토스 앱에서 미체결·잔량을 확인하세요." if args.command=="live-local" else "운영을 중지했습니다. 저장된 주문·포지션 상태는 다음 실행에서 복구됩니다."
        print(message, file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
