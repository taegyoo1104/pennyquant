"""Allow only recognized recovery edits before deploying a reviewed main commit."""
import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tarfile


def git(repo,*args):
    return subprocess.check_output(['git','-C',str(repo),*args])


def digest(data):return hashlib.sha256(data).hexdigest()


def deployment_mode_only(repo, name, path, current):
    """Older updaters chmod'ed every GCP shell script, including tracked 0644 files."""
    if Path(name).parent != Path('deploy/gcp') or Path(name).suffix != '.sh':
        return False
    if stat.S_IMODE(path.stat().st_mode) != 0o755:
        return False
    entry = git(repo, 'ls-tree', 'HEAD', '--', name)
    return (entry.startswith(b'100644 blob ') and
            current == digest(git(repo, 'show', 'HEAD:'+name)))


def known_recovery(repo,target):
    bundle=json.loads(git(repo,'show',target+':deploy/gcp/recovery/storage-fix-20261117.json'))
    packed=base64.b64decode(bundle['data'],validate=True)
    if digest(packed)!=bundle['sha256']:raise ValueError('복구 묶음 검증 실패')
    with tarfile.open(fileobj=io.BytesIO(packed)) as archive:
        return {m.name:digest(archive.extractfile(m).read()) for m in archive.getmembers()
                if m.isfile() and (m.name.startswith('deploy/gcp/') or m.name=='sessionquant/market/history.py')}


def plan(repo,target):
    if not re.fullmatch('[0-9a-f]{40}',target):raise ValueError('검토한 전체 커밋 SHA가 필요합니다.')
    subprocess.run(['git','-C',str(repo),'merge-base','--is-ancestor','HEAD',target],check=True)
    subprocess.run(['git','-C',str(repo),'merge-base','--is-ancestor',target,'origin/main'],check=True)
    if git(repo,'diff','--cached','--name-only').strip():raise ValueError('별도로 준비한 Git 변경이 있어 중단합니다.')
    for marker in ('MERGE_HEAD','rebase-merge','rebase-apply'):
        path=Path(git(repo,'rev-parse','--git-path',marker).decode().strip())
        if not path.is_absolute():path=repo/path
        if path.exists():raise ValueError('다른 Git 작업이 진행 중입니다.')
    targets={}
    for entry in git(repo,'ls-tree','-r','-z',target).split(b'\0'):
        if entry:
            info,path=entry.split(b'\t',1);targets[path.decode()]=info.decode().split()[0]
    changed=[p.decode() for p in git(repo,'diff','--name-only','-z','HEAD').split(b'\0') if p]
    tracked={p.decode() for p in git(repo,'ls-files','-z').split(b'\0') if p}
    collisions=[p for p in targets if p not in tracked and (repo/p).exists()]
    known=known_recovery(repo,target)
    verified={}
    for name in dict.fromkeys([*changed,*collisions]):
        if not re.fullmatch(r'[A-Za-z0-9_./-]+',name) or '..' in Path(name).parts:
            raise ValueError('확인할 수 없는 파일 이름입니다.')
        path=repo/name
        if (not path.is_file() or path.is_symlink() or targets.get(name) not in {'100644','100755'}
                or any(parent.is_symlink() for parent in path.parents if parent!=repo.parent)):
            raise ValueError('직접 확인할 변경 파일: '+name)
        current=digest(path.read_bytes())
        expected=digest(git(repo,'show',target+':'+name))
        if current not in {expected,known.get(name)} and not (
                name in tracked and deployment_mode_only(repo,name,path,current)):
            raise ValueError('알려진 복구 수정과 다른 파일이므로 보존하고 중단합니다: '+name)
        verified[name]={'sha256':current,'untracked':name in collisions,
                        'mode':stat.S_IMODE(path.stat().st_mode)}
    return {'head':git(repo,'rev-parse','HEAD').decode().strip(),'target':target,'files':verified}


def apply(repo,target,backup):
    checked=plan(repo,target)
    backup.mkdir(mode=0o700,parents=False,exist_ok=False)
    (backup/'manifest.json').write_text(json.dumps(checked,indent=2)+'\n')
    # Copy every affected file before touching the checkout. No .env or DB paths.
    for name,record in checked['files'].items():
        destination=backup/'files'/name;destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(repo/name,destination)
        if digest(destination.read_bytes())!=record['sha256']:raise ValueError('백업 파일 검증 실패')
    # Revalidate after backup; reject files changed during preparation.
    if plan(repo,target)!=checked:raise ValueError('준비 중 파일이 바뀌어 중단합니다.')
    for name,record in checked['files'].items():
        if record['untracked']:(repo/name).unlink()  # Exact copy retained in private backup.
        else:git(repo,'restore','--source=HEAD','--worktree','--',name)
    # Detached deployment preserves all existing branch tips and pins the tested code.
    git(repo,'checkout','--detach','--no-overwrite-ignore',target)
    print('코드 갱신 완료. 이전 변경 백업: '+str(backup))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',type=Path,required=True)
    parser.add_argument('--target',required=True)
    parser.add_argument('--backup',type=Path)
    args=parser.parse_args()
    try:
        if args.backup:apply(args.repo.resolve(),args.target,args.backup)
        else:
            result=plan(args.repo.resolve(),args.target)
            print('변경 검사 통과: 보존할 복구 수정 '+str(len(result['files']))+'개')
    except (ValueError,subprocess.CalledProcessError,OSError,KeyError) as exc:
        print(str(exc) if isinstance(exc,ValueError) else 'Git·파일 검사 실패. 기존 파일을 확인하세요.')
        raise SystemExit(1)


if __name__=='__main__':main()
