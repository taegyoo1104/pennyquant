"""로컬 비밀정보의 재사용·우선순위·화면 격리·비활성화 기본값을 검증한다."""
from pathlib import Path
import os
import pytest
from sessionquant.local_env import load_local_env,save_local_env,setup_local_env,env_file

@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    from sessionquant.local_env import CONNECTION_KEYS
    for key in CONNECTION_KEYS|{'SQ_ENV_FILE'}:
        monkeypatch.setenv(key,'test-isolation-placeholder')
        monkeypatch.delenv(key)


def test_secret_roundtrip_keeps_dollars_hash_quotes_backslashes(tmp_path):
    value="비밀번호 $a ${HOME} # ' \\ $() `command`"
    target=tmp_path/'.env'
    save_local_env({'SQ_MYSQL_PASSWORD':value},path=target)
    loaded=load_local_env(path=target)
    assert loaded==('SQ_MYSQL_PASSWORD',)
    assert os.environ['SQ_MYSQL_PASSWORD']==value


def test_explicit_environment_wins_over_saved_file(tmp_path,monkeypatch):
    target=tmp_path/'.env'; target.write_text('TOSS_CLIENT_SECRET=saved\n')
    monkeypatch.setenv('TOSS_CLIENT_SECRET','explicit')
    assert load_local_env(path=target)==()
    assert os.environ['TOSS_CLIENT_SECRET']=='explicit'


def test_dashboard_loads_database_credentials_without_broker_or_model_keys(tmp_path):
    target=tmp_path/'.env'
    save_local_env({'SQ_MYSQL_PASSWORD':'db-secret','TOSS_CLIENT_SECRET':'broker-secret','OPENAI_API_KEY':'model-secret'},path=target)
    load_local_env(path=target,database_only=True)
    assert os.environ['SQ_MYSQL_PASSWORD']=='db-secret'
    assert 'TOSS_CLIENT_SECRET' not in os.environ and 'OPENAI_API_KEY' not in os.environ


def test_file_cannot_set_execution_flags_or_process_paths(tmp_path):
    target=tmp_path/'.env'
    target.write_text('SQ_EXECUTE=true\nLIVE_ENABLED=true\nPYTHONPATH=malicious\n')
    assert load_local_env(path=target)==()
    assert 'SQ_EXECUTE' not in os.environ and 'LIVE_ENABLED' not in os.environ


def test_capture_current_values_does_not_print_secrets_or_store_activation(tmp_path,monkeypatch,capsys):
    monkeypatch.setenv('TOSS_CLIENT_ID','PRIVATE-ID')
    monkeypatch.setenv('TOSS_CLIENT_SECRET','PRIVATE-SECRET')
    monkeypatch.setenv('SQ_MYSQL_PASSWORD','PRIVATE-PASSWORD')
    monkeypatch.setenv('SQ_ENVIRONMENT','LIVE_LOCAL')
    monkeypatch.setenv('SQ_DB_NAMESPACE','test-live')
    target=setup_local_env(from_current=True,path=tmp_path/'.env')
    text=capsys.readouterr().out
    assert all(secret not in text for secret in ('PRIVATE-ID','PRIVATE-SECRET','PRIVATE-PASSWORD'))
    assert 'LIVE_LOCAL' not in target.read_text() and 'SQ_DB_NAMESPACE' not in target.read_text()
    assert 'PAPER_LOCAL' in target.read_text()


def test_setup_can_reuse_current_toss_settings_without_retyping(tmp_path,monkeypatch):
    monkeypatch.setenv('TOSS_CLIENT_ID','saved-id')
    monkeypatch.setenv('TOSS_CLIENT_SECRET','saved-secret')
    target=setup_local_env(path=tmp_path/'.env',input_fn=lambda prompt:'',secret_fn=lambda prompt:'')
    monkeypatch.delenv('TOSS_CLIENT_ID'); monkeypatch.delenv('TOSS_CLIENT_SECRET')
    load_local_env(path=target)
    assert os.environ['TOSS_CLIENT_ID']=='saved-id' and os.environ['TOSS_CLIENT_SECRET']=='saved-secret'


def test_explicit_file_and_utf8_bom_are_supported(tmp_path,monkeypatch):
    target=tmp_path/'.env'; target.write_text('SQ_MYSQL_PASSWORD="암호"\n',encoding='utf-8-sig')
    monkeypatch.setenv('SQ_ENV_FILE',str(target))
    assert env_file()==target
    load_local_env()
    assert os.environ['SQ_MYSQL_PASSWORD']=='암호'


def test_missing_file_is_optional(tmp_path):
    assert load_local_env(path=tmp_path/'missing')==()


def test_saved_file_permissions_and_unknown_settings_are_preserved(tmp_path):
    target=tmp_path/'.env'; target.write_text('CUSTOM_NOTE=preserved\n')
    save_local_env({'SQ_MYSQL_PASSWORD':'secret'},path=target)
    assert 'CUSTOM_NOTE=preserved' in target.read_text()
    if os.name!='nt': assert target.stat().st_mode & 0o077 == 0


def test_new_python_process_reuses_saved_values_without_shell_input(tmp_path):
    import subprocess
    import sys
    from sessionquant.local_env import CONNECTION_KEYS
    target=save_local_env({'TOSS_CLIENT_SECRET':'local-only-value','SQ_MYSQL_PASSWORD':'saved-db-value'},path=tmp_path/'.env')
    child_env={k:v for k,v in os.environ.items() if k not in CONNECTION_KEYS}
    child_env['SQ_ENV_FILE']=str(target)
    child_env['PYTHONPATH']=str(Path(__file__).resolve().parents[1])
    code="from sessionquant.local_env import load_local_env; import os; load_local_env(); assert os.environ['TOSS_CLIENT_SECRET']=='local-only-value'; assert os.environ['SQ_MYSQL_PASSWORD']=='saved-db-value'; print('loaded')"
    result=subprocess.run([sys.executable,'-c',code],env=child_env,cwd=tmp_path,capture_output=True,text=True)
    assert result.returncode==0 and result.stdout.strip()=='loaded'
    assert 'saved-db-value' not in result.stdout


def test_default_project_env_path_does_not_depend_on_pycharm_working_directory(tmp_path,monkeypatch):
    import sessionquant.local_env as module
    monkeypatch.chdir(tmp_path)
    assert env_file()==Path(module.__file__).resolve().parents[1]/'.env'
