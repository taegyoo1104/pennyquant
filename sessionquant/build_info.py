"""Image-local build provenance; never infer running code from Git, DB or env."""
from datetime import datetime, timezone
import argparse
import json
from pathlib import Path
import re
import tomllib


MANIFEST = Path(__file__).with_name('_build_info.json')
SOURCES = {'reviewed_archive', 'working_tree', 'unknown'}
REVISION = re.compile(r'[0-9a-f]{40}')
VERSION = re.compile(r'[A-Za-z0-9.!+_-]{1,80}')


def _validated(value):
    if not isinstance(value, dict) or type(value.get('schema_version')) is not int or value['schema_version'] != 1:
        raise ValueError('Invalid build manifest')
    source, revision = value.get('source'), value.get('revision')
    version = value.get('version')
    if source not in SOURCES or not isinstance(version, str) or not VERSION.fullmatch(version):
        raise ValueError('Invalid build manifest')
    if revision is not None and (not isinstance(revision, str) or not REVISION.fullmatch(revision)):
        raise ValueError('Invalid build revision')
    if (source == 'reviewed_archive' and revision is None) or (source == 'unknown' and revision is not None):
        raise ValueError('Inconsistent build provenance')
    stamp = datetime.fromisoformat(value['built_at'])
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError('Build time requires timezone')
    return {'schema_version': 1, 'source': source, 'revision': revision,
            'version': version, 'built_at': stamp.astimezone(timezone.utc).isoformat()}


def read_build_info(path=None):
    """A missing/old/corrupt manifest is unknown, never the checkout's HEAD."""
    try:
        with (Path(path) if path is not None else MANIFEST).open('rb') as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ValueError('Oversized build manifest')
        return _validated(json.loads(raw))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return {'schema_version': 1, 'source': 'unknown', 'revision': None,
                'version': None, 'built_at': None}


def write_build_info(destination, *, project_file, source='unknown', revision='', now=None):
    """Called while building the image, before installing its wheel/package."""
    project = tomllib.loads(Path(project_file).read_text(encoding='utf-8'))['project']
    if project.get('name') != 'sessionquant':
        raise ValueError('Expected SessionQuant project')
    stamp = now or datetime.now(timezone.utc)
    value = _validated({'schema_version': 1, 'source': source,
                        'revision': None if source == 'unknown' or not revision else revision,
                        'version': project['version'], 'built_at': stamp.isoformat()})
    Path(destination).write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n', encoding='utf-8')
    return value


def main():
    parser = argparse.ArgumentParser(description='이미지에 빌드 시점의 코드 정보를 기록합니다.')
    parser.add_argument('--write', type=Path, required=True)
    parser.add_argument('--project-file', type=Path, default=Path('pyproject.toml'))
    parser.add_argument('--source', choices=sorted(SOURCES), default='unknown')
    parser.add_argument('--revision', default='')
    args = parser.parse_args()
    write_build_info(args.write, project_file=args.project_file, source=args.source, revision=args.revision)


if __name__ == '__main__':
    main()
