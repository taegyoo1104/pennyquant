"""Git에 올리지 않는 로컬 접속 설정. 값의 확장·명령 실행·실주문 활성화는 하지 않는다."""
from __future__ import annotations

import getpass
import os
from pathlib import Path

DB_KEYS=frozenset({'SQ_DB_BACKEND','SQ_ENVIRONMENT','SQ_DB_NAMESPACE',
    *('SQ_MYSQL_'+suffix for suffix in ('HOST','PORT','DATABASE','USER','PASSWORD')),
    'PQ_DB_BACKEND','PQ_ENVIRONMENT','PQ_DB_NAMESPACE',
    *('PQ_MYSQL_'+suffix for suffix in ('HOST','PORT','DATABASE','USER','PASSWORD'))})
CONNECTION_KEYS=DB_KEYS|frozenset({'TOSS_CLIENT_ID','TOSS_CLIENT_SECRET','TOSS_ACCOUNT_SEQ',
    'APCA_API_KEY_ID','APCA_API_SECRET_KEY','OPENAI_API_KEY','OPENAI_MODEL','SEC_USER_AGENT','FRED_API_KEY',
    'PQ_DB','PQ_REPORT','SQ_DB'})
DEFAULTS={'SQ_DB_BACKEND':'mysql','SQ_ENVIRONMENT':'PAPER_LOCAL','SQ_MYSQL_HOST':'127.0.0.1',
    'SQ_MYSQL_PORT':'3306','SQ_MYSQL_DATABASE':'sessionquant','SQ_MYSQL_USER':'sessionquant'}


def env_file() -> Path:
    """실행 위치가 바뀌어도 editable 프로젝트의 .env를 찾는다."""
    override=os.environ.get('SQ_ENV_FILE')
    if override: return Path(override).expanduser().resolve()
    root=Path(__file__).resolve().parent.parent
    return root/'.env' if (root/'pyproject.toml').is_file() else Path.cwd()/'.env'


def load_local_env(*, path: Path | None = None, database_only: bool = False) -> tuple[str,...]:
    """기존 환경변수를 우선하며 화면에는 DB 접속 설정만 노출한다."""
    target=Path(path) if path is not None else env_file()
    if not target.is_file(): return ()
    from dotenv import dotenv_values
    # $와 ${...}를 비밀번호 일부로 취급하며 셸·다른 변수로 확장하지 않는다.
    values=dotenv_values(target,encoding='utf-8-sig',interpolate=False)
    allowed=DB_KEYS if database_only else CONNECTION_KEYS
    loaded=[]
    for name,value in values.items():
        if name in allowed and value is not None and value!='' and name not in os.environ:
            os.environ[name]=value
            loaded.append(name)
    return tuple(loaded)


def save_local_env(values: dict[str,str],*,path: Path | None = None) -> Path:
    """확인된 접속 변수만 저장하며 기존 파일의 다른 설정은 보존한다."""
    from dotenv import set_key
    target=Path(path) if path is not None else env_file()
    if target.is_symlink(): raise ValueError('설정 저장에는 심볼릭 링크 대신 실제 .env 파일을 사용하세요.')
    target.parent.mkdir(parents=True,exist_ok=True)
    if not target.exists():
        fd=os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        os.close(fd)
    for name,value in values.items():
        if name in CONNECTION_KEYS and isinstance(value,str) and value!='':
            set_key(str(target),name,value,quote_mode='always',encoding='utf-8')
    return target


def setup_local_env(*,from_current=False,path: Path | None = None,input_fn=None,secret_fn=None,news_only=False) -> Path:
    """현재 연결값을 저장하거나 로컬 터미널에서 한 번 입력받는다."""
    input_fn=input_fn or input
    secret_fn=secret_fn or getpass.getpass
    values=dict(DEFAULTS)
    for key in CONNECTION_KEYS:
        if key in {'SQ_ENVIRONMENT','PQ_ENVIRONMENT','SQ_DB_NAMESPACE','PQ_DB_NAMESPACE'}: continue
        value=os.environ.get(key)
        if value: values[key]=value
    for key in DEFAULTS:
        if key.startswith('SQ_') and key not in os.environ and 'PQ_'+key[3:] in values:
            values[key]=values['PQ_'+key[3:]]
    prompts=(
        ('OPENAI_API_KEY','OpenAI API 키(LLM 분석)',True),
        ('FRED_API_KEY','FRED API 키(일별 거시 참고자료, 선택)',True),
        ('APCA_API_KEY_ID','Alpaca API Key ID(선택)',True),
        ('APCA_API_SECRET_KEY','Alpaca API Secret Key(선택)',True),
        ('SEC_USER_AGENT','SEC 식별자: ASCII 프로젝트명과 실제 연락 이메일',False)) if news_only else (
        ('TOSS_CLIENT_ID','토스 클라이언트 ID',True),
        ('TOSS_CLIENT_SECRET','토스 클라이언트 시크릿',True),
        ('TOSS_ACCOUNT_SEQ','토스 계좌 순번(선택)',False),
        ('SQ_MYSQL_HOST','MySQL 호스트',False),('SQ_MYSQL_PORT','MySQL 포트',False),
        ('SQ_MYSQL_DATABASE','MySQL DB 이름',False),('SQ_MYSQL_USER','MySQL 사용자',False),
        ('SQ_MYSQL_PASSWORD','MySQL 전용 계정 비밀번호',True))
    if not from_current:
        for key,label,secret in prompts:
            existing=values.get(key)
            suffix=' (기존 값 유지: Enter)' if existing and secret else f' [{existing}]' if existing else ' (미설정, 선택 항목은 Enter)'
            value=(secret_fn if secret else input_fn)(label+suffix+': ')
            if value: values[key]=value
    # 최근 실거래 화면을 열었다는 이유로 다음 기본 DB 조회 환경을 실거래로 저장하지 않는다.
    values['SQ_ENVIRONMENT']='PAPER_LOCAL'
    target=save_local_env(values,path=path)
    required=('OPENAI_API_KEY','SEC_USER_AGENT') if news_only else ('TOSS_CLIENT_ID','TOSS_CLIENT_SECRET','SQ_MYSQL_PASSWORD')
    configured=[key for key in required if values.get(key)]
    print(f'로컬 접속 설정을 저장했습니다: {target}')
    print(f'필수 설정 저장: {len(configured)}/{len(required)}개. 실제 값은 출력하지 않습니다.')
    if len(configured)<len(required): print('누락된 값은 setup-news 또는 setup-env를 실행해 한 번 입력하세요.')
    print('다음 실행부터 자동으로 읽습니다. 실주문 활성화는 저장하지 않습니다.')
    return target
