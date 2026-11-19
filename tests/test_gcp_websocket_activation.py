"""Profile switch uses the original ledger guard and a fresh backup before editing."""
import importlib.util
import os
from pathlib import Path
import subprocess

import pytest

from test_gcp_deployment import DEPLOY, executable


def helper():
    spec=importlib.util.spec_from_file_location('websocket_profile',DEPLOY/'websocket_profile.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_atomic_profile_change_preserves_secrets_and_private_original(tmp_path):
    h=helper();env=tmp_path/'app.env'
    old=b'SQ_MYSQL_PASSWORD="literal$secret"\nSQ_DB_NAMESPACE=old-paper\nOTHER=value\n'
    env.write_bytes(old)
    raw,already,fingerprint=h.inspect(env)
    assert raw==old and not already
    h.apply(env,tmp_path/'backup',fingerprint)
    assert (tmp_path/'backup/app.env').read_bytes()==old
    assert (env.stat().st_mode & 0o777)==0o600
    assert (tmp_path/'backup').stat().st_mode & 0o777==0o700
    result=env.read_text()
    assert 'SQ_MYSQL_PASSWORD="literal$secret"' in result and 'OTHER=value' in result
    assert result.count('SQ_DB_NAMESPACE=')==1 and h.inspect(env)[1]
    with pytest.raises(ValueError):h.apply(env,tmp_path/'backup2',fingerprint)


@pytest.mark.parametrize('contents',[
    'SQ_DB_NAMESPACE=one\nSQ_DB_NAMESPACE=two\n',
    'SQ_DB_NAMESPACE=sessionquant-strategy-lab-ws-v1\n',
    'SQ_SESSION_CONFIG=config/sessionquant.strategy-lab-websocket.example.yaml\n',
])
def test_ambiguous_or_partial_profile_is_not_changed(tmp_path,contents):
    env=tmp_path/'app.env';env.write_text(contents)
    with pytest.raises(ValueError):helper().inspect(env)
    assert env.read_text()==contents


def test_changed_settings_and_symlink_are_not_overwritten(tmp_path):
    h=helper();env=tmp_path/'app.env';env.write_text('OTHER=old\n')
    _,_,fingerprint=h.inspect(env)
    env.write_text('OTHER=new\n')
    with pytest.raises(ValueError):h.apply(env,tmp_path/'backup',fingerprint)
    assert env.read_text()=='OTHER=new\n' and not (tmp_path/'backup').exists()
    link=tmp_path/'link';link.symlink_to(env)
    with pytest.raises(ValueError):h.inspect(link)


def test_target_requires_empty_or_stopped_compatible_ledger():
    h=helper()
    assert h.verify_target(None,None,None,False)
    runtime={'version':2,'environment':'PAPER_LOCAL','source':'toss','identity':'toss-readonly-websocket-v1',
        'live_orders_enabled':False,'synthetic':False}
    state={'schema_version':3,'synthetic':False,'environment':'PAPER_LOCAL',
           'strategy':'pullback_simple','paper_data_policy':'observed_paper_v1'}
    control={'mode':'paper','status':'OFFLINE','armed':False,'running':False}
    assert h.verify_target(runtime,state,control,True)
    for args in [(None,None,None,True),({**runtime,'identity':'toss-readonly'},state,control,True),
                 (runtime,state,{**control,'armed':True},True),
                 (runtime,state,{**control,'status':'WAITING'},True),
                 (runtime,state,control,True,1)]:
        with pytest.raises(ValueError):h.verify_target(*args)


def fixture(tmp_path,mode):
    root=tmp_path/'repo';deploy=root/'deploy/gcp';deploy.mkdir(parents=True)
    etc=tmp_path/'etc';etc.mkdir();env=etc/'app.env';env.write_text('SQ_DB_NAMESPACE=old\nSECRET=keep\n')
    (etc/'backup-status').write_text('UPLOADED old backup')
    run=tmp_path/'run';run.mkdir();data=tmp_path/'data';data.mkdir()
    bindir=tmp_path/'bin';bindir.mkdir();calls=tmp_path/'calls'
    (deploy/'compose.yaml').touch();(deploy/'update_guard.py').write_text('# original ledger guard\n')
    helper_text=(DEPLOY/'websocket_profile.py').read_text().replace('/etc/sessionquant',str(etc))
    (deploy/'websocket_profile.py').write_text(helper_text)
    executable(deploy/'backup.sh',f'''echo backup >> "$CALLS"
if [[ "$MODE" == backup-fail ]]; then exit 8; fi
if [[ "$MODE" == backup-stale ]]; then exit 0; fi
printf 'UPLOADED fresh archive\n' > '{etc}/backup-status'
''')
    executable(bindir/'docker',f'''echo "docker $*" >> "$CALLS"
if [[ "$MODE" == window-fail && "$*" == run* ]]; then exit 4; fi
if [[ "$*" == *" python - --stopped"* ]]; then
    cat >/dev/null
    [[ "$MODE" != guard-fail && "$MODE" != rollback-fail ]] || exit 7
    [[ $(python3 '{deploy}/websocket_profile.py' inspect) != ALREADY ]] || exit 9
elif [[ "$*" == *" python - probe-target"* ]]; then
    cat >/dev/null
    [[ "$MODE" != target-fail ]] || exit 6
fi
''')
    executable(bindir/'systemctl',f'''echo "systemctl $*" >> "$CALLS"
if [[ "$*" == is-active* && "$*" == *service ]]; then exit 3; fi
if [[ "$*" == 'start sessionquant.service' ]]; then
    selected=$(python3 '{deploy}/websocket_profile.py' inspect)
    echo "start-profile $selected" >> "$CALLS"
    if [[ "$MODE" == start-fail && "$selected" == ALREADY ]]; then exit 5; fi
    if [[ "$MODE" == rollback-fail ]]; then exit 9; fi
fi
''')
    script=tmp_path/'activate.sh'
    text=(DEPLOY/'activate_websocket.sh').read_text()
    text='\n'.join(line for line in text.splitlines() if not line.startswith('[[ $EUID == 0'))+'\n'
    text=text.replace('/opt/sessionquant',str(root)).replace('/etc/sessionquant',str(etc))
    text=text.replace('/run/sessionquant-update.lock',str(run/'update.lock')).replace('/var/lib/sessionquant',str(data))
    script.write_text(text)
    environ={**os.environ,'PATH':str(bindir)+os.pathsep+os.environ['PATH'],'CALLS':str(calls),'MODE':mode}
    return script,environ,env,calls,data


@pytest.mark.skipif(os.name=='nt',reason='Linux deployment scripts')
@pytest.mark.parametrize('mode',['success','guard-fail','target-fail','backup-fail','backup-stale',
                                'start-fail','rollback-fail','window-fail'])
def test_switch_order_and_failure_preserve_original_ledger(tmp_path,mode):
    script,environ,env,calls,data=fixture(tmp_path,mode)
    old=env.read_bytes()
    result=subprocess.run(['bash',str(script)],env=environ,capture_output=True,text=True,timeout=15)
    lines=calls.read_text().splitlines()
    if mode=='window-fail':
        assert result.returncode==4 and env.read_bytes()==old
        assert not any(row.startswith('systemctl stop') for row in lines)
    elif mode in {'guard-fail','target-fail','backup-fail','backup-stale','rollback-fail'}:
        assert result.returncode!=0 and env.read_bytes()==old
        assert any(row.startswith('start-profile ') and 'ALREADY' not in row for row in lines)
        assert not list(data.glob('update-backups/*/app.env'))
        if mode=='rollback-fail':
            assert result.returncode==7 and '원래 서비스 재시작 실패' in result.stderr
        if mode=='backup-stale':assert '새 Cloud Storage 백업' in result.stderr
    else:
        assert result.returncode==(5 if mode=='start-fail' else 0),result.stderr
        assert helper().inspect(env)[1]
        originals=list(data.glob('update-backups/*/app.env'))
        assert len(originals)==1 and originals[0].read_bytes()==old
        guarded=next(i for i,row in enumerate(lines) if 'python - --stopped' in row)
        assert guarded<lines.index('backup')<lines.index('start-profile ALREADY')
        if mode=='start-fail':assert '프로필을 유지' in result.stderr
        else:
            again=subprocess.run(['bash',str(script)],env=environ,capture_output=True,text=True,timeout=15)
            assert again.returncode==0 and '이미 웹소켓' in again.stdout
            assert calls.read_text().splitlines().count('backup')==1
