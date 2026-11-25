# 문서 지도

SessionQuant는 **OpenAI Codex와 함께 기획·구현·검증하는 모의매매 프로젝트**입니다. 처음 방문했다면 [README](../README.md)를 먼저 읽으세요. 이 안내는 설명서·운영 절차·기술 참고·과거 기록을 구분합니다.

현재 권장 실행은 [웹소켓 전략 실험 설정](../config/sessionquant.strategy-lab-websocket.example.yaml)을 명시적으로 지정하는 방식입니다. 예전 설정도 비교와 복구를 위해 남아 있으며, 명령의 기본값과 권장 설정은 다를 수 있습니다.

## 프로젝트를 이해하고 싶다면

| 궁금한 내용 | 문서 |
| --- | --- |
| 무엇을 만들었고 왜 Codex를 사용했나? | [프로젝트 소개](../README.md) |
| 처음 기획이 왜 달라졌나? 실제로 무엇이 어려웠나? | [기획 변화와 현실적인 제한](PROJECT_STORY.md) |
| 종목은 어떻게 뽑고 언제 사고파나? | [매매 과정](TRADING_PROCESS.md) |
| 눌림목·호가·RVOL·웹소켓이 무슨 뜻인가? | [쉬운 용어 사전](GLOSSARY.md) |
| 전략의 정확한 조건과 LLM의 역할은? | [전략 실험 모드](STRATEGY_LAB.md) |

## 직접 실행하거나 운영한다면

| 목적 | 문서 |
| --- | --- |
| 설치, API 없이 첫 재현 시나리오 실행 | [시작 안내](GETTING_STARTED.md) |
| 실제 데이터로 모의매매, 자료 준비, 매매 시작·종료, 기록 확인 | [매일 운영하기](REALTIME_PAPER.md) |
| Windows CMD → PuTTY → tmux, Tailscale 접속, 업데이트·마감 백업 | [GCP 운영](GCP_PAPER.md#windows-cmd에서-시작하기) |
| 기존 REST 프로필에서 웹소켓 프로필로 전환 | [웹소켓 전환](WEBSOCKET_ROLLOUT.md) |
| 분봉 지연이 API·DB·계산 중 어디서 생겼는지 확인 | [부담을 제한한 지연 진단](WEBSOCKET_ROLLOUT.md#다음-운영에서-분봉-지연-확인하기) |
| 구버전 VM이 켜진 직후 종료되어 최초 업데이트가 어려움 | [최초 점검 부팅](GCP_OFFHOURS_MIGRATION.md) |
| 디스크 부족·백업 시간 초과의 근거와 복구 절차 | [저장 공간·백업 장애 기록](GCP_STORAGE_RECOVERY.md) |

프로그램 시작, 매매 시작, 코드 업데이트, 대시보드 연결은 다른 작업입니다. 운영 절차에 표시한 **로컬 PC / Cloud Shell / 운영 VM**을 먼저 확인하고 명령을 실행하세요.

## 코드를 이해하거나 수정한다면

[개발 안내](DEVELOPMENT.md)는 전체 구조와 검사를 설명하고, [HANDOFF](../HANDOFF.md)는 다음 개발 작업을 위한 현재 기준을 정리합니다.

| 분야 | 상세 자료 | 읽을 때 구분할 점 |
| --- | --- | --- |
| 저장 | [MySQL 설정](reference/MYSQL_LOCAL.md), [DB에 저장하는 것](reference/DATABASE_STORAGE.md) | 가상 장부와 실제 계좌, 실행별 저장 공간 구분 |
| 시세·거래량 | [감시 목록과 RVOL](reference/WATCHLIST_RVOL.md), [분봉 API 확인](reference/TOSS_HISTORY_PROBE.md) | 과거 거래량 비교는 현재 단순 전략의 필수 진입 조건이 아님 |
| 뉴스·AI | [뉴스·AI 설정](reference/NEWS_SETUP.md), [기업 RSS](reference/COMPANY_RSS.md) | 기사 수집, 모델 호출, 검증된 결과는 서로 다른 단계 |
| 화면 | [대시보드 참고](reference/MAIN_DASHBOARD.md) | 메인 요약과 상세 근거, 모의 자금과 실제 계좌 구분 |
| 개발 계약 | [입력 형식](reference/EVENT_FORMAT.md), [API 확인 기록](reference/API_VERIFICATION.md) | API 지원 여부와 실제 계정에서 확인한 범위 구분 |
| 별도 검증 경로 | [실거래 제한 검증](reference/PHASE5_LOCAL.md) | 일반 모의매매 실행 안내가 아니므로 현재 권장 경로와 분리 |

## 과거 기록은 따로 읽습니다

[과거 문서 안내](archive/README.md)는 초기 PennyQuant 기획, 단계별 보고, 옛 README·인수인계를 Git 이력의 고정된 원문으로 연결합니다. 중복된 과거 파일 31개는 현재 작업 트리에서 정리했습니다. 이전 요구사항과 복구 근거가 사라진 것은 아닙니다. 과거 문서는 현재 운영 매뉴얼로 사용하지 않습니다.

## 문서의 기준

- **현재 권장 동작:** 명시한 설정 파일과 연결된 소스 코드가 기준입니다. 실행 옵션을 바꾸면 결과도 달라집니다.
- **운영 확인:** 실제 서버 적용 여부는 배포된 버전과 실행 설정·로그로 확인합니다. 문서 수정이나 GitHub 병합만으로 VM이 업데이트되지는 않습니다.
- **검증 결과:** 합성 자료 테스트, API 조회 확인, 실제 장중 운영 기록을 구분합니다. 모의 체결이나 테스트 통과가 수익성을 증명하는 것은 아닙니다.
- **관리 방법:** 새로운 기능은 관련 안내에 반영하고, README 앞에 날짜별 작업 로그를 계속 붙이지 않습니다.
