"""Read one small host report; no SQL, API requests or filesystem scans."""
from datetime import datetime, timezone
import json
from pathlib import Path

from ..models import utc

REPORT = Path('/app/health/storage.json')


def load_report(path=REPORT):
    try:
        if not path.exists():
            return {}
        if path.stat().st_size > 16384:
            raise ValueError('oversized')
        report = json.loads(path.read_text())
        if not isinstance(report, dict) or report.get('version') != 1:
            raise ValueError('unsupported')
        return report
    except (OSError, ValueError):
        return {'status': 'UNKNOWN'}


def messages(report, now=None):
    if not report:
        return []
    now = now or datetime.now(timezone.utc)
    result = []
    try:
        age = (now - utc(report['checked_at'])).total_seconds()
        if not 0 <= age <= 180:
            result.append('저장 공간 점검 기록이 오래됐습니다. 최신 상태를 확인하세요.')
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        result.append('저장 공간 점검 시각을 확인할 수 없습니다.')
    status = report.get('status')
    if status in {'LOW', 'WARNING'}:
        result.append('백업에 필요한 여유 공간이 부족합니다.' if status == 'LOW'
                      else '백업 여유 공간이 줄고 있습니다. 장 마감 전에 확인하세요.')
    elif status != 'OK':
        result.append('백업에 필요한 공간을 확인하지 못했습니다.')
    if str(report.get('backup_status', '')).startswith('FAILED '):
        result.append('최근 백업이 실패했습니다. 이전 백업은 최신 거래 기록을 포함하지 않을 수 있습니다.')
    if str(report.get('retention_status', '')).startswith('SKIPPED '):
        result.append('최근 자료 정리는 백업 실패로 실행하지 못했습니다. 아래 과거 정리 결과와 구분하세요.')
    elif str(report.get('retention_status', '')).startswith(('FAILED ', 'PARTIAL ')):
        result.append('최근 자료 정리가 끝나지 않았습니다. 남은 대상과 종료 이유를 확인하세요.')
    return result


def render_storage_health(st, *, detailed=False):
    report = load_report()
    for message in messages(report):
        st.warning(message)
    if detailed and report:
        free, needed = report.get('free_bytes'), report.get('required_free_bytes')
        if all(type(x) is int and x >= 0 for x in (free, needed)):
            st.caption(f'백업 공간: 여유 {free / 1024**3:.1f} GiB · 예상 필요 {needed / 1024**3:.1f} GiB. '
                       '압축 DB·최종 백업·예비 공간을 합한 추정치이며, 실제 생성 중에도 공간을 검사합니다.')
        st.caption('DB 파일 크기와 남아 있는 자료 양은 다릅니다. 삭제한 공간은 DB가 재사용할 수 있으며 파일이 즉시 작아지지는 않습니다.')
        files = report.get('largest_database_files')
        if isinstance(files, list):
            rows = [{'DB 파일': item['file'], '파일 크기(GiB)': round(item['bytes']/1024**3, 2)}
                    for item in files[:7] if isinstance(item, dict) and isinstance(item.get('file'), str)
                    and type(item.get('bytes')) is int and item['bytes'] >= 0]
            if rows:
                st.dataframe(rows, hide_index=True, width='stretch')
