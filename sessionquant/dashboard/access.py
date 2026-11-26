"""Server-side dashboard roles. Public mode is opt-in; no identity comes from URLs/headers."""
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from collections import deque

SESSION_SECONDS = 3600
_GRANT = '_sq_admin_grant'
_LOCK = threading.Lock()
_ATTEMPTS = deque(maxlen=5)
_SIGNING_KEY = secrets.token_bytes(32)


def protected():
    # Unknown configured values fail closed, instead of silently enabling local admin.
    return os.environ.get('SQ_DASHBOARD_ACCESS', 'local') != 'local'


def shared_dashboard():
    return os.environ.get('SQ_DASHBOARD_ACCESS') == 'shared'


def can_browse():
    """Reading pages never grants permission to send commands or save settings."""
    return shared_dashboard() or is_admin()


def password_hash(password):
    if not 16 <= len(password) <= 256:
        raise ValueError('관리자 비밀번호는 16~256자로 설정하세요.')
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return 'scrypt$'+salt.hex()+'$'+digest.hex()


def verify_password(password, encoded):
    try:
        scheme, salt, expected = encoded.split('$')
        if scheme != 'scrypt' or len(salt) != 32 or len(expected) != 128 or not 16 <= len(password) <= 256:
            return False
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


def credential():
    return os.environ.get('SQ_DASHBOARD_ADMIN_HASH', '')


def _signature(expiry, config):
    return hmac.new(_SIGNING_KEY, f'{expiry}:{config}'.encode(), 'sha256').hexdigest()


def authenticated(state, *, now=None):
    now = time.monotonic() if now is None else now
    grant = state.get(_GRANT)
    if not isinstance(grant, tuple) or len(grant) != 2 or not credential():
        return False
    expiry, signature = grant
    return (type(expiry) is float and now < expiry <= now + SESSION_SECONDS
            and isinstance(signature, str) and hmac.compare_digest(signature, _signature(expiry, credential())))


def login(state, password, *, now=None):
    now = time.monotonic() if now is None else now
    with _LOCK:
        while _ATTEMPTS and now - _ATTEMPTS[0] >= 60:
            _ATTEMPTS.popleft()
        if len(_ATTEMPTS) >= 5:
            return False
        _ATTEMPTS.append(now)
        valid = verify_password(password, credential())
    if valid:
        expiry = float(now + SESSION_SECONDS)
        state[_GRANT] = (expiry, _signature(expiry, credential()))
    logging.getLogger(__name__).info('dashboard_admin_login result=%s', 'accepted' if valid else 'denied')
    return valid


def is_admin():
    if os.environ.get('SQ_DASHBOARD_ACCESS', 'local') not in {'local', 'admin', 'shared'}:
        return False
    if not protected():
        return True
    from streamlit.runtime.scriptrunner import get_script_run_ctx
    if get_script_run_ctx(suppress_warning=True) is None:
        return False
    import streamlit as st
    return authenticated(st.session_state)


def require_admin():
    if not is_admin():
        raise PermissionError('관리자 로그인이 필요합니다. 다시 로그인한 뒤 요청하세요.')


def render_login():
    import streamlit as st
    if os.environ.get('SQ_DASHBOARD_ACCESS') not in {'admin', 'shared'}:
        return
    if is_admin():
        st.caption('관리자 · 로그인 유효시간 1시간')
        if st.button('로그아웃', key='sq_logout'):
            st.session_state.pop(_GRANT, None)
            st.rerun()
        return
    st.caption('공개 조회 · 실제 시세 기반 모의매매')
    with st.expander('관리자 로그인'):
        if not credential():
            st.info('관리자 접속이 아직 설정되지 않았습니다.')
            return
        def submit():
            password = st.session_state.pop('sq_admin_password', '')
            st.session_state['_sq_login_failed'] = not login(st.session_state, password)
        with st.form('sq_login', clear_on_submit=True):
            st.text_input('관리자 비밀번호', type='password', key='sq_admin_password', max_chars=256)
            st.form_submit_button('로그인', on_click=submit)
        if st.session_state.get('_sq_login_failed'):
            st.warning('로그인하지 못했습니다. 비밀번호를 확인하고 잠시 후 다시 시도하세요.')
