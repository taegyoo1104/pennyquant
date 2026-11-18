"""Keep the existing .tar.gz layout without recompressing the compressed SQL."""

import gzip
from pathlib import Path
import sys
import tarfile


def package(database, app, destination):
    database, app, destination = map(Path, (database, app, destination))
    if not database.is_file() or not database.stat().st_size or not app.is_dir():
        raise ValueError("압축 DB와 앱 자료 디렉터리를 확인하세요.")
    # SQL has already been compressed and verified by backup.sh. A level-zero
    # gzip envelope preserves compatibility with restore/review tools while
    # avoiding a second compression pass over the large SQL archive.
    with destination.open("xb") as output:
        with gzip.GzipFile(filename="", fileobj=output, mode="wb", compresslevel=0, mtime=0) as packed:
            with tarfile.open(fileobj=packed, mode="w|") as archive:
                archive.add(database, arcname="database.sql.gz")
                archive.add(app, arcname="app")


if __name__ == "__main__":
    package(*sys.argv[1:])
