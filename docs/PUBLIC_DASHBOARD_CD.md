# 공개 대시보드와 자동 배포

SessionQuant의 실제 시세 기반 **모의매매** 운영 화면을 공개합니다. 방문자는 자산·보유 종목·전략 단계·체결 기록·시장 차트·AI 브리핑을 볼 수 있습니다. 관리자 조작과 원본 진단 자료는 공개 화면에 포함하지 않습니다.

## 접속 구조

| 용도 | 주소 | 접근 |
| --- | --- | --- |
| 공개 조회 | `https://sessionquant-paper.tailf05449.ts.net:8443` | Tailscale 설치·로그인 없이 누구나 |
| 관리자 | `https://sessionquant-paper.tailf05449.ts.net` | 같은 tailnet에 연결한 본인 기기 + 관리자 비밀번호 |

도메인 구매나 GCP의 8501·8502 포트 개방 없이 Tailscale Funnel이 HTTPS 공개 연결을 제공합니다. 둘 다 GCP VM에서 실행되므로 VM은 켜져 있어야 합니다. PC·PuTTY는 꺼도 됩니다. Funnel은 Tailscale 정책상 활성화가 필요할 수 있고 대역폭 제한이 있으므로, 대규모 서비스용 호스팅을 대신하지는 않습니다.

- 공개 컨테이너는 `public.env`만 받고 **메인 화면에 필요한 5개 테이블의 SELECT 전용 DB 계정**을 사용합니다. 뉴스 원본·AI 감사 로그·시스템 로그 테이블은 읽을 수 없습니다. 관리자 비밀번호·거래 API 키·파일 볼륨을 받지 않습니다. 프로세스 자체에서 `viewer` 역할을 강제합니다.
- 공개 화면은 15초마다 갱신하며 DB 조회 결과를 10초 공유 캐시로 보관합니다. 방문자마다 전체 진단 자료를 읽지 않습니다. 휴장·폐장에는 마지막 기록을 보여줍니다. 백업·정리 중 DB가 응답하지 않으면 준비 중 안내를 표시합니다.
- 관리자 로그인은 서버 세션에만 유지됩니다. 비밀번호는 scrypt 해시로 저장하고, 1시간 만료·로그아웃·프로세스 재시작·비밀번호 교체 시 다시 로그인합니다. 전체 프로세스에서 1분에 최대 5번 로그인 검사를 허용합니다.
- 버튼을 숨기는 것과 별도로 명령 저장 직전 권한을 검사합니다. 명령에는 `requested_by`가 기록됩니다. 청산 확인 절차는 유지합니다. 공개 조회 경로에는 관리자 로그인이 없습니다.
- 점검 화면은 관리자라도 매매 엔진을 시작하지 않습니다. 운영일에는 기존 절차로 매매 프로그램을 켜고 관리자 화면에서 시작합니다. 보호 모드의 매매 시작 명령은 서버에서 정규장 달력을 다시 확인합니다.

## 최초 설정 (휴장·폐장, 매매 종료 후)

먼저 이 변경이 포함된 버전을 기존 고정 SHA 업데이트 절차로 배포합니다. 아직 공개된 상태가 아닙니다. 기존 관리자 Tailscale Serve는 443 → `127.0.0.1:8501` 그대로 유지합니다.

Windows CMD에서 VM 접속:

```cmd
gcloud compute ssh sessionquant-paper --project=sessionquant --zone=us-central1-a --tunnel-through-iap
```

PuTTY에서 실행합니다. 기존 매매·정리 작업이 끝난 상태여야 합니다.

```bash
sudo bash /opt/sessionquant/deploy/gcp/public_enable.sh
```

16자 이상 관리자 비밀번호를 두 번 입력합니다. 화면·로그·명령행에는 비밀번호가 남지 않습니다. 명령이 표시하는 Tailscale 정책 승인 안내가 있다면 승인 후 다시 실행합니다. 마지막 완료 메시지를 확인한 뒤:

1. 휴대폰의 Tailscale을 끄고 공개 주소 `:8443`에 접속합니다. 차트와 모의 자산이 보이고 매매 버튼·관리자 로그인·시스템 메뉴가 없어야 합니다.
2. Tailscale을 끈 상태에서 기존 관리자 주소(포트 없음)는 접속되지 않아야 합니다.
3. Tailscale을 켜고 기존 관리자 주소에서 관리자 로그인을 확인합니다. 휴장·점검 상태에서는 매매가 시작되지 않습니다.

현재 설정 도구는 관리자 계정 1개를 사용합니다. `public_enable.sh`를 다시 실행하면 비밀번호와 공개 DB 계정을 교체합니다. 처음 설정과 마찬가지로 매매 종료 후 실행하세요. 바뀐 DB 계정 때문에 이전 백업의 재사용 증명은 무효화됩니다.

## CD 활성화

```bash
sudo bash /opt/sessionquant/deploy/gcp/cd_enable.sh
```

GitHub Actions의 SSH 비밀키나 장기 GCP 키를 추가하지 않습니다. VM이 5분마다 공개 저장소를 확인하는 **pull 방식 CD**입니다.

1. `main`의 정확한 커밋에 대해 `.github/workflows/ci.yml`의 최신 push 검사가 성공했는지 확인합니다. PR 검사만 통과한 커밋, 실패·진행 중인 재검사는 배포하지 않습니다.
2. 휴장·정규장 마감 후이며 매매 프로그램이 실행 중이지 않아야 합니다. OFFLINE 상태, 보유·미체결·청산 요청 없음, 백업·정리 작업 없음도 확인합니다. 조건이 맞지 않으면 `DEFERRED`로 기다립니다. 장전에도 자동 배포는 기다립니다.
3. 기존 업데이트 스크립트의 잠금·검증·백업·자료 정리 절차를 그대로 실행합니다. 매매 시작이나 무조건 백업 생략은 하지 않습니다.
4. 성공 후 **점검 화면만** 켜고 공개 컨테이너를 새 이미지로 갱신합니다. 버전·HTTP 응답·공개 DB 조회·Funnel 경로를 확인합니다.
5. 실패·프로세스 강제 종료 시 시도 표시를 남기고 다음 자동 시도를 차단합니다. 새 커밋이 올라와도 백업을 반복 생성하지 않습니다. DB 변경 가능성이 있으므로 자동 코드 되돌리기는 하지 않습니다.

실행 중 작업이 오래 걸려도 다음 타이머가 중복 실행하지 않습니다. 업데이트 서비스의 전체 시간 한도는 3시간입니다. 배포 중 관리자 화면은 잠시 중단되며, 별도 공개 컨테이너는 가능한 동안 기존 기록을 제공합니다. 공개 화면 교체 시에는 잠시 재연결됩니다.

확인:

```bash
sudo cat /var/lib/sessionquant/cd/status.json
sudo systemctl list-timers sessionquant-cd.timer --no-pager
sudo journalctl -u sessionquant-cd.service -n 30 --no-pager
sudo tail -n 40 /var/lib/sessionquant/cd/deploy.log
```

`CURRENT`: 최신 코드 / `DEFERRED`: 안전 조건 대기 / `COMPLETE`: 배포 완료 / `FAILED`·`BLOCKED`: 수동 확인 필요. 첫 검사 전에는 상태 파일이 없을 수 있습니다. 서비스 로그와 배포 로그는 관리자 전용입니다.

실패 후 로그를 검토하고 기존 복구 절차로 운영 상태를 확인한 다음에만 재시도 차단을 해제합니다:

```bash
sudo systemctl stop sessionquant-cd.timer
# 실행 중 배포가 없는지 확인. 실행 중 서비스를 강제로 끊지 않습니다.
sudo systemctl status sessionquant-cd.service --no-pager
# 원인 해결·복구 확인 후에만 실행:
sudo rm /var/lib/sessionquant/cd/attempt.json
sudo systemctl start sessionquant-cd.timer
```

자동 배포 중지: `sudo systemctl disable --now sessionquant-cd.timer` (실행 중 배포는 끝까지 진행).

공개 접속만 중지:

```bash
sudo tailscale funnel --https=8443 off
sudo rm -f /etc/sessionquant/public-enabled
sudo docker compose -f /opt/sessionquant/deploy/gcp/compose.public.yaml down
```

관리자 Tailscale 접속·DB·매매 기록은 삭제하지 않습니다.
