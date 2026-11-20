"""Show the running dashboard's baked image identity without a remote lookup."""
from datetime import datetime, timedelta, timezone

from ..build_info import read_build_info


def version_summary(info):
    source = info['source']
    revision = info['revision']
    built_at = info['built_at']
    return {
        'version': info['version'] or '미확인',
        'revision_label': '실행 코드' if source == 'reviewed_archive' else '빌드 기준 커밋',
        'revision': revision,
        'revision_url': f'https://github.com/taegyoo1104/pennyquant/commit/{revision}' if revision else None,
        'source_label': {'reviewed_archive': '지정한 커밋으로 만든 이미지',
                         'working_tree': '작업 폴더로 만든 이미지 · 수정 포함 가능',
                         'unknown': '코드 버전 미확인'}[source],
        'built_at': datetime.fromisoformat(built_at).astimezone(timezone(timedelta(hours=9))).strftime('%Y-%m-%d %H:%M:%S KST')
                    if built_at else '미확인',
    }


def render_version(st):
    summary = version_summary(read_build_info())
    st.subheader('실행 중인 프로그램')
    st.write('프로그램 버전 · ' + summary['version'])
    st.caption(summary['source_label'])
    if summary['revision']:
        st.markdown(f"{summary['revision_label']} · [{summary['revision'][:12]}]({summary['revision_url']})")
    else:
        st.caption('실행 코드 정보를 확인할 수 없습니다. 버전 정보가 없는 기존 이미지나 일반 빌드일 수 있습니다.')
    st.caption('이미지 생성 시각 · ' + summary['built_at'])
    st.caption('현재 화면 서버가 실행 중인 이미지 기준입니다. GitHub 최신 여부와 GCP 배포 완료 여부를 자동 판정하지 않습니다.')
