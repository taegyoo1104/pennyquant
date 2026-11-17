# 문서 안내

처음 보는 사람은 [프로젝트 소개](../README.md)부터 읽으면 됩니다. 현재 사용 안내와 과거 개발 기록을 분리했습니다.

| 필요한 내용 | 읽을 문서 |
| --- | --- |
| 설치·데모·PyCharm 실행 | [시작 안내](GETTING_STARTED.md) |
| 실제 데이터 모의매매·예열·뉴스 확인·종료 후 분석 | [운영 안내](REALTIME_PAPER.md) |
| GCP 서버 한 대·클라우드 DB 이전·자동 종료·백업 | [저비용 GCP 안내](GCP_PAPER.md) |
| 코드 구조·설정·테스트·현재 남은 검증 | [개발 안내](DEVELOPMENT.md) |
| 다음 Codex 작업에서 이어받을 현재 상태 | [HANDOFF](../HANDOFF.md) |

## 필요한 경우에만 보는 상세 자료

- 데이터: [MySQL 설정](reference/MYSQL_LOCAL.md), [저장 항목·용량](reference/DATABASE_STORAGE.md), [감시 목록과 거래량 비교](reference/WATCHLIST_RVOL.md), [분봉 API 검사](reference/TOSS_HISTORY_PROBE.md)
- 뉴스: [뉴스·AI 설정](reference/NEWS_SETUP.md), [기업 RSS 목록](reference/COMPANY_RSS.md)
- 개발: [입력 자료 형식](reference/EVENT_FORMAT.md), [외부 API 확인 기록](reference/API_VERIFICATION.md)
- 별도 기능: [실제 계좌 조회 화면](reference/MAIN_DASHBOARD.md), [실거래 검증 경로](reference/PHASE5_LOCAL.md). 기본 모의매매 실행과는 별개입니다.

## 과거 기록

[archive](archive)에는 이전 안내와 검토 기록, [archive/stages](archive/stages)에는 단계별 구현 기록, [archive/legacy](archive/legacy)에는 초기 PennyQuant 기획과 실행 안내를 보관했습니다. 당시 종목 수·명령·화면·완료 상태가 현재와 다를 수 있습니다. 현재 사용 방법은 위의 시작·운영 안내를 기준으로 합니다.

문서 정리 이전의 [README](archive/README_2026-11-15.md)와 [상세 HANDOFF](archive/HANDOFF_2026-11-15.md)도 보관했습니다. 앞으로 사용자 안내는 시작·운영 문서에, 개발자가 이어받을 사항은 짧은 HANDOFF에 반영합니다.
