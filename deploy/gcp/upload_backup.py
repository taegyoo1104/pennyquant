"""GCE 서비스 계정으로 비공개 버킷에 백업 한 개를 스트리밍 업로드한다."""
import http.client
import json
from pathlib import Path
import re
import sys
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


def upload(path, bucket):
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]', bucket):
        raise ValueError('백업 버킷 이름을 확인하세요.')
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError('완료된 백업 파일이 없습니다.')
    request = Request('http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token',
                      headers={'Metadata-Flavor': 'Google'})
    with urlopen(request, timeout=10) as response:
        if response.headers.get('Metadata-Flavor') != 'Google':
            raise ValueError('GCE 서비스 계정 응답을 확인할 수 없습니다.')
        token = json.load(response)['access_token']
    query = urlencode({'uploadType': 'media', 'name': 'daily/' + path.name, 'ifGenerationMatch': '0'})
    conn = http.client.HTTPSConnection('storage.googleapis.com', timeout=120)
    try:
        with path.open('rb') as source:
            size = path.stat().st_size
            conn.putrequest('POST', '/upload/storage/v1/b/' + quote(bucket, safe='') + '/o?' + query)
            conn.putheader('Authorization', 'Bearer ' + token)
            conn.putheader('Content-Type', 'application/gzip')
            conn.putheader('Content-Length', str(size))
            conn.endheaders()
            while chunk := source.read(1024 * 1024):
                conn.send(chunk)
        response = conn.getresponse()
        body = response.read(65536)
        if response.status not in (200, 201):
            raise RuntimeError(f'백업 업로드 실패 HTTP {response.status}')
        if int(json.loads(body).get('size', -1)) != size:
            raise RuntimeError('백업 업로드 크기 확인 실패')
    finally:
        conn.close()
    print('Cloud Storage 백업 완료:', path.name)


if __name__ == '__main__':
    try:
        upload(sys.argv[1], Path('/etc/sessionquant/backup-bucket').read_text().strip())
    except Exception as exc:
        # Exception text may contain remote details; never print credentials or bodies.
        print('백업 업로드 실패:', type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
