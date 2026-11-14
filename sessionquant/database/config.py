"""로컬 MySQL 설정과 실행 환경 구분. 비밀번호는 URL이나 로그에 넣지 않는다."""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import os
from pathlib import Path, PureWindowsPath
import re
from typing import Mapping

ENVIRONMENTS = ("PAPER_LOCAL", "LIVE_LOCAL", "LIVE_GCP")


def setting(env: Mapping[str, str], suffix: str, default: str = "") -> str:
    """새 SQ_ 설정을 우선하고 기존 PQ_ 설정도 읽는다."""
    return env.get("SQ_" + suffix, env.get("PQ_" + suffix, default))


def database_backend(environ: Mapping[str, str] | None = None) -> str:
    env = environ if environ is not None else os.environ
    value = setting(env, "DB_BACKEND", "sqlite").strip().lower()
    if value not in {"sqlite", "mysql"}:
        raise ValueError("SQ_DB_BACKEND / PQ_DB_BACKEND 값은 sqlite 또는 mysql이어야 합니다")
    return value


def namespace_for(path: str | Path, override: str | None = None, environment: str | None = None) -> str:
    value = str(path)
    if "://" in value:
        raise ValueError("DB URL 대신 MySQL 환경변수(environment variables)를 사용하세요")
    if override is not None:
        if not isinstance(override, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", override):
            raise ValueError("SQ_DB_NAMESPACE 값은 영문·숫자·밑줄·하이픈 1~64자로 지정하세요")
        stem = override.lower()
    else:
        stem = (PureWindowsPath(value) if "\\" in value else Path(value)).stem.lower() or "default"
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", stem):
            slug = re.sub(r"[^a-z0-9_-]", "_", stem).strip("_-") or "data"
            stem = f"{slug[:48]}_{sha256(stem.encode('utf-8')).hexdigest()[:12]}"
    if environment is not None:
        if environment not in ENVIRONMENTS:
            raise ValueError("SQ_ENVIRONMENT 값은 PAPER_LOCAL, LIVE_LOCAL, LIVE_GCP 중 하나여야 합니다")
        prefixed = environment.lower() + "_" + stem
        if len(prefixed) > 64:
            prefixed = prefixed[:51] + "_" + sha256(prefixed.encode()).hexdigest()[:12]
        return prefixed
    return stem


@dataclass(frozen=True)
class MySQLConfig:
    host: str = "127.0.0.1"
    port: int = 3306
    database: str = "sessionquant"
    user: str = "sessionquant"
    password: str = field(default="", repr=False)
    namespace: str | None = None
    environment: str | None = None
    connect_timeout: int = 10
    read_timeout: int = 30
    write_timeout: int = 30

    def __post_init__(self):
        if (not isinstance(self.host, str) or not self.host or len(self.host) > 255
                or any(char.isspace() or ord(char) < 33 for char in self.host)
                or any(char in self.host for char in "/@\\")):
            raise ValueError("SQ_MYSQL_HOST 값은 호스트 이름 또는 IP 주소여야 합니다")
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ValueError("SQ_MYSQL_PORT / PQ_MYSQL_PORT 값은 1~65535 정수여야 합니다")
        if not isinstance(self.database, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", self.database):
            raise ValueError("SQ_MYSQL_DATABASE 값은 영문으로 시작하는 영문·숫자·밑줄 1~64자여야 합니다")
        if not isinstance(self.user, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", self.user):
            raise ValueError("SQ_MYSQL_USER 값은 영문·숫자·점·밑줄·하이픈 1~32자여야 합니다")
        if not isinstance(self.password, str) or not self.password:
            raise ValueError("로컬 환경변수 SQ_MYSQL_PASSWORD / PQ_MYSQL_PASSWORD를 설정하세요")
        namespace_for("system.db", self.namespace, self.environment)
        for value in (self.connect_timeout, self.read_timeout, self.write_timeout):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 300:
                raise ValueError("MySQL 연결 제한시간은 1~300초 정수여야 합니다")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> MySQLConfig:
        env = environ if environ is not None else os.environ
        try:
            port = int(setting(env, "MYSQL_PORT", "3306"))
        except (TypeError, ValueError):
            raise ValueError("SQ_MYSQL_PORT / PQ_MYSQL_PORT 값은 1~65535 정수여야 합니다") from None
        # 예전 PQ_ 설정만 사용하는 경우 기존 DB 이름을 유지한다.
        legacy = any(key.startswith("PQ_MYSQL_") for key in env) and not any(key.startswith("SQ_MYSQL_") for key in env)
        default_name = "pennyquant" if legacy else "sessionquant"
        return cls(host=setting(env, "MYSQL_HOST", "127.0.0.1"), port=port,
                   database=setting(env, "MYSQL_DATABASE", default_name), user=setting(env, "MYSQL_USER", default_name),
                   password=setting(env, "MYSQL_PASSWORD"), namespace=setting(env, "DB_NAMESPACE") or None,
                   environment=setting(env, "ENVIRONMENT") or None)
