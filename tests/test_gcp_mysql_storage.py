"""Opt-in isolated MySQL 8.4 purge/restart/restore regression. Never uses production data."""
import os
import pytest

@pytest.mark.skipif(os.getenv('SQ_TEST_GCP_STORAGE') != '1', reason='requires local Docker MySQL fixture')
def test_mysql_binlog_cleanup_disable_and_compressed_restore():
    import subprocess, time, uuid, gzip, tarfile, tempfile, importlib.util
    from pathlib import Path
    suffix=uuid.uuid4().hex[:12]
    name='sq-storage-test-'+suffix
    volume=name+'-data'
    def run(*args, data=None):
        return subprocess.run(args,input=data,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True).stdout
    def sql(query):
        return run('docker','exec','-i',name,'sh','-c','MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -u root -N -B',data=query.encode()).decode()
    def start(flag):
        run('docker','run','-d','--name',name,'--network','none','--mount','type=volume,source='+volume+',target=/var/lib/mysql',
            '-e','MYSQL_ROOT_PASSWORD=fixture-only','-e','MYSQL_DATABASE=sessionquant','mysql:8.4',flag,'--innodb-buffer-pool-size=128M','--performance-schema=OFF')
        deadline=time.monotonic()+100
        while time.monotonic()<deadline:
            try:
                if sql('SELECT 1').strip()=='1':return
            except subprocess.CalledProcessError:pass
            time.sleep(1)
        raise RuntimeError('fixture DB not ready')
    try:
        run('docker','volume','create',volume)
        start('--log-bin=binlog')
        sql('CREATE TABLE sessionquant.fixture (id INT PRIMARY KEY, body LONGTEXT); INSERT INTO sessionquant.fixture VALUES (1, REPEAT("x", 100000)); FLUSH BINARY LOGS;')
        purge=(Path(__file__).parents[1]/'deploy/gcp/purge_binlogs.sh').read_text().split("<<'SH'\n",1)[1].split('\nSH',1)[0]
        print(run('docker','exec','-i',name,'sh','-eu',data=purge.encode()).decode())
        assert len(sql('SHOW BINARY LOGS').strip().splitlines())==1
        assert sql('SELECT LENGTH(body) FROM sessionquant.fixture').strip()=='100000'
        run('docker','stop',name);run('docker','rm',name)
        start('--skip-log-bin')
        assert sql('SELECT @@GLOBAL.log_bin').strip()=='0'
        dump=run('docker','exec',name,'sh','-c','MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysqldump -u root --single-transaction --quick --no-tablespaces --set-gtid-purged=OFF sessionquant')
        # The replacement packager keeps compatibility with existing logical
        # restore paths, including a real MySQL dump and restored table values.
        with tempfile.TemporaryDirectory() as work:
            work=Path(work);database=work/'database.sql.gz';database.write_bytes(gzip.compress(dump))
            app=work/'app';app.mkdir();(app/'state.json').write_text('{"fixture":true}')
            spec=importlib.util.spec_from_file_location('package_backup',Path(__file__).parents[1]/'deploy/gcp/package_backup.py')
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            module.package(database,app,work/'backup.tar.gz')
            with tarfile.open(work/'backup.tar.gz','r:gz') as archive:
                packed=archive.extractfile('database.sql.gz').read()
                assert archive.extractfile('app/state.json').read()==b'{"fixture":true}'
        sql('CREATE DATABASE restore_check;')
        run('docker','exec','-i',name,'sh','-c','MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -u root restore_check',data=gzip.decompress(packed))
        assert sql('SELECT COUNT(*),MAX(LENGTH(body)) FROM restore_check.fixture').strip()=='1\t100000'
        print('PASS: MySQL 8.4 purge, table preservation, restart log_bin=0, gzip dump and restore')
    finally:
        subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        subprocess.run(['docker','volume','rm',volume],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
