"""Display provenance and preserve local recovery edits during a pinned update."""
import base64
from copy import deepcopy
from datetime import timedelta
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile

import pytest

from sessionquant.dashboard.reference_panel import reference_rows
from sessionquant.market_reference.contracts import observation
from test_market_reference import NOW

ROOT=Path(__file__).parents[1]


def test_received_etf_price_is_visible_without_promoting_analysis():
    price=observation('SPY','price',{'price':'600','currency':'USD'},NOW,NOW,NOW,proof={})
    view={'items':{'SPY':{'quality_status':'UNKNOWN','latest_observations':{'price':price}}}}
    before=deepcopy(view)
    row=next(r for r in reference_rows(view,NOW) if r['종목']=='SPY')
    assert row['관측 가격']=='$600.00' and row['가격 수신']=='최근 수신'
    assert row['분석 상태']=='확인 필요' and row['5분 ETF 등락률(%)'] is None
    stale=next(r for r in reference_rows(view,NOW+timedelta(minutes=10)) if r['종목']=='SPY')
    assert stale['관측 가격']=='$600.00' and stale['가격 수신']=='과거 기록'
    assert view==before


@pytest.mark.parametrize('fault',['future','invalid','currency','revision'])
def test_bad_reference_observation_is_not_displayed_as_a_price(fault):
    price=observation('SPY','price',{'price':'600','currency':'USD'},NOW,NOW,NOW,proof={})
    if fault=='future':price['received_at']=(NOW+timedelta(seconds=1)).isoformat()
    if fault=='invalid':price['price']='NaN'
    if fault=='currency':price['currency']='KRW'
    if fault=='revision':price['quality_status']='REVISION_PENDING'
    row=next(r for r in reference_rows({'items':{'SPY':{'latest_observations':{'price':price}}}},NOW) if r['종목']=='SPY')
    assert row['관측 가격']=='—'


def test_risk_quote_fallback_is_labelled_midpoint_without_analysis_permission():
    quotes={'SPY':{'symbol':'SPY','bid':'599','ask':'601','timestamp':NOW.isoformat(),'received_at':NOW.isoformat()}}
    row=next(r for r in reference_rows({},NOW,quotes=quotes) if r['종목']=='SPY')
    assert row['관측 가격']=='$600.00' and row['가격 종류']=='호가 중간값'
    assert row['분석 상태']=='확인 필요'
    quotes['SPY']['bid']='602'
    row=next(r for r in reference_rows({},NOW,quotes=quotes) if r['종목']=='SPY')
    assert row['관측 가격']=='—'


def updater():
    spec=importlib.util.spec_from_file_location('prepare_update',ROOT/'deploy/gcp/prepare_update.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


@pytest.fixture
def checkout(tmp_path):
    repo=tmp_path/'repo';repo.mkdir()
    def run(*args):return subprocess.check_output(['git','-C',str(repo),*args],stderr=subprocess.DEVNULL).decode().strip()
    run('init','-b','main');run('config','user.name','Fixture');run('config','user.email','fixture@example.invalid')
    history=repo/'sessionquant/market/history.py';history.parent.mkdir(parents=True)
    history.write_text('base\n');(repo/'.gitignore').write_text('.env\ndata/\n')
    run('add','.');run('commit','-m','base');base=run('rev-parse','HEAD')
    history.write_text('new verified code\n')
    start=repo/'deploy/gcp/start.sh';start.parent.mkdir(parents=True);start.write_text('new start\n')
    packed=io.BytesIO()
    with tarfile.open(fileobj=packed,mode='w:gz') as tar:
        for name,body in [('sessionquant/market/history.py',b'old recovery code\n'),('deploy/gcp/start.sh',b'old recovery start\n')]:
            info=tarfile.TarInfo(name);info.size=len(body);tar.addfile(info,io.BytesIO(body))
    bundle=repo/'deploy/gcp/recovery/storage-fix-20261117.json';bundle.parent.mkdir()
    data=packed.getvalue();bundle.write_text(json.dumps({'sha256':hashlib.sha256(data).hexdigest(),'data':base64.b64encode(data).decode()}))
    run('add','.');run('commit','-m','target');target=run('rev-parse','HEAD')
    run('update-ref','refs/remotes/origin/main',target);run('checkout','--detach',base)
    history.write_text('old recovery code\n');start.parent.mkdir(parents=True,exist_ok=True);start.write_text('old recovery start\n')
    (repo/'.env').write_text('PRIVATE');(repo/'data').mkdir();(repo/'data/db').write_bytes(b'db-sentinel')
    return repo,base,target,run


def test_known_edits_are_backed_up_and_update_preserves_environment_and_database(checkout,tmp_path):
    repo,base,target,run=checkout
    module=updater();plan=module.plan(repo,target)
    assert set(plan['files'])=={'sessionquant/market/history.py','deploy/gcp/start.sh'}
    backup=tmp_path/'backup';module.apply(repo,target,backup)
    assert run('rev-parse','HEAD')==target
    assert (backup/'files/sessionquant/market/history.py').read_text()=='old recovery code\n'
    assert (backup/'files/deploy/gcp/start.sh').read_text()=='old recovery start\n'
    assert (repo/'.env').read_text()=='PRIVATE' and (repo/'data/db').read_bytes()==b'db-sentinel'
    assert json.loads((backup/'manifest.json').read_text())['head']==base
    assert module.plan(repo,target)['files']=={}


@pytest.mark.parametrize('mode',['unknown','staged','symlink'])
def test_unknown_or_staged_edits_stop_before_checkout(checkout,tmp_path,mode):
    repo,base,target,run=checkout
    history=repo/'sessionquant/market/history.py'
    if mode=='unknown':history.write_text('operator customization\n')
    if mode=='staged':run('add','sessionquant/market/history.py')
    if mode=='symlink':history.unlink();history.symlink_to(repo/'.env')
    with pytest.raises(ValueError):updater().apply(repo,target,tmp_path/'backup')
    assert run('rev-parse','HEAD')==base and not (tmp_path/'backup').exists()
    assert (repo/'.env').read_text()=='PRIVATE'


def test_prepare_check_and_update_shells_parse_and_do_not_delete_runtime_data():
    for name in ('update_preopen.sh','preopen_check.sh'):
        path=ROOT/'deploy/gcp'/name
        subprocess.run(['bash','-n',str(path)],check=True)
        text=path.read_text()
        assert 'down --volumes' not in text and 'reset --hard' not in text
    check=(ROOT/'deploy/gcp/preopen_check.sh').read_text()
    assert 'SELECT @@GLOBAL.log_bin' in check and 'check-toss' in check
    assert 'news-refresh' not in check and 'llm' not in check.split('set -Eeuo',1)[-1].lower()


@pytest.mark.parametrize('failure',['','build','backup','check','masked'])
def test_update_sequence_does_not_start_app_on_failed_checks(tmp_path,failure):
    import os
    from test_gcp_deployment import executable
    repo=tmp_path/'repo';deploy=repo/'deploy/gcp';deploy.mkdir(parents=True)
    stage=tmp_path/'stage';(stage/'deploy/gcp').mkdir(parents=True)
    etc=tmp_path/'etc';etc.mkdir();(etc/'app.env').write_text('PRIVATE')
    data=tmp_path/'data';data.mkdir();(data/'database').write_text('PRESERVED')
    calls=tmp_path/'calls';binpath=tmp_path/'bin';binpath.mkdir()
    (deploy/'sessionquant.service').write_text('fixture unit')
    (stage/'deploy/gcp/sessionquant.service').write_text('fixture unit')
    for folder in (deploy,stage/'deploy/gcp'):
        (folder/'sessionquant-cost-guard.timer').write_text('fixture timer')
        (folder/'prepare_update.py').write_text('')
        (folder/'storage_check.py').write_text('')
    executable(deploy/'backup.sh','echo backup >> "$CALLS"\n[[ "$FAILURE" != backup ]]\n')
    executable(deploy/'preopen_check.sh','echo check >> "$CALLS"\n[[ "$FAILURE" != check ]]\n')
    executable(binpath/'hostname','echo sessionquant-paper\n')
    executable(binpath/'git','echo "git $*" >> "$CALLS"\n')
    executable(binpath/'tar','cat >/dev/null\n')
    executable(binpath/'mktemp','echo "$STAGE"\n')
    executable(binpath/'python3','echo "python $*" >> "$CALLS"\n')
    executable(binpath/'systemctl','''echo "systemctl $*" >> "$CALLS"
if [[ "$1" == is-active && "$*" == *service* ]]; then exit 3; fi
if [[ "$1" == is-enabled && "$FAILURE" == masked ]]; then echo masked; fi
exit 0
''')
    executable(binpath/'docker','''echo "docker $*" >> "$CALLS"
if [[ "$1" == build && "$FAILURE" == build ]]; then exit 1; fi
if [[ "$*" == *"SELECT @@GLOBAL.log_bin"* ]]; then echo 0; fi
if [[ "$*" == "image inspect"* ]]; then exit 1; fi
exit 0
''')
    (tmp_path/'product').write_text('Google Compute Engine')
    units=tmp_path/'units';units.mkdir()
    script=tmp_path/'update.sh'
    text=(ROOT/'deploy/gcp/update_preopen.sh').read_text()
    for before,after in [('/opt/sessionquant',str(repo)),('/etc/sessionquant',str(etc)),
        ('/var/lib/sessionquant',str(data)),('/etc/systemd/system',str(units)),
        ('/run/sessionquant-update.lock',str(tmp_path/'lock')),('/sys/class/dmi/id/product_name',str(tmp_path/'product')),
        ('$EUID == 0','0 == 0')]:text=text.replace(before,after)
    script.write_text(text)
    result=subprocess.run(['bash',str(script),'a'*40],capture_output=True,text=True,timeout=15,
        env={**os.environ,'PATH':str(binpath)+':'+os.environ['PATH'],'CALLS':str(calls),'STAGE':str(stage),'FAILURE':failure})
    log=calls.read_text()
    assert (result.returncode==0)==(not failure),result.stderr
    assert ('systemctl start sessionquant.service' in log)==(not failure)
    assert (etc/'app.env').read_text()=='PRIVATE' and (data/'database').read_text()=='PRESERVED'
    if failure in ('build','masked'):assert 'systemctl stop sessionquant.service' not in log
    if not failure:
        assert log.index('docker build')<log.index('systemctl stop sessionquant.service')<log.index('backup\n')
        assert log.index('backup\n')<log.index('--backup ')<log.index('check\n')<log.index('systemctl start sessionquant.service')
        assert 'is-active --quiet sessionquant-market-close.timer' in log
        assert 'is-active --quiet sessionquant-storage-guard.timer' in log


@pytest.mark.parametrize('failure',['','toss','binlog','packet','backup'])
def test_preopen_checks_refuse_invalid_database_backup_or_toss(tmp_path,failure):
    import os
    from test_gcp_deployment import executable
    repo=tmp_path/'repo';(repo/'deploy/gcp').mkdir(parents=True)
    (repo/'deploy/gcp/storage_check.py').write_text('')
    etc=tmp_path/'etc';etc.mkdir();(etc/'backup-status').write_text(('FAILED' if failure=='backup' else 'UPLOADED')+' fixture')
    binpath=tmp_path/'bin';binpath.mkdir();calls=tmp_path/'calls'
    executable(binpath/'hostname','echo sessionquant-paper\n')
    executable(binpath/'docker','''echo "docker $*" >> "$CALLS"
if [[ "$*" == *"SELECT @@GLOBAL.log_bin"* ]]; then
  echo "$DB_VALUES"
else
  echo "$TOSS_RESULT"
  exit "$TOSS_EXIT"
fi
''')
    script=tmp_path/'check.sh'
    script.write_text((ROOT/'deploy/gcp/preopen_check.sh').read_text().replace('/opt/sessionquant',str(repo))
        .replace('/etc/sessionquant',str(etc)).replace('/var/tmp/',str(tmp_path)+'/').replace('$EUID == 0','0 == 0'))
    result=subprocess.run(['bash',str(script)],capture_output=True,text=True,timeout=15,env={**os.environ,
        'PATH':str(binpath)+':'+os.environ['PATH'],'CALLS':str(calls),
        'DB_VALUES':f"{int(failure=='binlog')} {4194304 if failure=='packet' else 67108864} 43",
        'TOSS_EXIT':'1' if failure=='toss' else '0',
        'TOSS_RESULT':json.dumps({'success':failure!='toss','live_orders_enabled':False,'error':{'http_status':403}})})
    assert (result.returncode==0)==(not failure),result.stderr
    if failure=='toss':assert 'HTTP 403' in result.stdout
    assert not list(tmp_path.glob('sq-toss-check.*'))
    log=calls.read_text()
    assert ' run ' not in log or 'check-toss --symbol AAPL' in log
