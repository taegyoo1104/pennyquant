from dataclasses import replace

import pytest

from sessionquant.database.config import MySQLConfig, database_backend, namespace_for


def test_database_selection_is_explicit_and_does_not_require_mysql_for_sqlite():
    assert database_backend({}) == "sqlite"
    assert database_backend({"PQ_DB_BACKEND": "MySQL"}) == "mysql"
    with pytest.raises(ValueError, match="PQ_DB_BACKEND"):
        database_backend({"PQ_DB_BACKEND": "unknown"})


def test_mysql_password_is_not_a_url_or_repr_and_preserves_actual_value():
    secret = "PRIVATE-p@ss: with spaces!"
    config = MySQLConfig.from_env({"PQ_MYSQL_PASSWORD": secret})
    assert config.password == secret and secret not in repr(config)
    assert config.host == "127.0.0.1" and config.port == 3306
    assert config.database == config.user == "pennyquant"
    with pytest.raises(ValueError, match="PQ_MYSQL_PASSWORD"):
        MySQLConfig.from_env({})


@pytest.mark.parametrize("field,value", [
    ("port", 0), ("port", True), ("database", "pennyquant;DROP_DATABASE"),
    ("database", "has-dash"), ("user", "user@localhost"), ("host", "mysql://PRIVATE@localhost"),
    ("namespace", "system:live"), ("read_timeout", 0),
])
def test_mysql_config_rejects_malformed_identifiers_without_echoing_secrets(field, value):
    with pytest.raises(ValueError) as exc:
        replace(MySQLConfig(password="private"), **{field: value})
    assert "PRIVATE" not in str(exc.value)


def test_bad_port_has_a_fixed_message_instead_of_raw_input():
    with pytest.raises(ValueError) as exc:
        MySQLConfig.from_env({"PQ_MYSQL_PORT": "PRIVATE", "PQ_MYSQL_PASSWORD": "secret"})
    assert "PQ_MYSQL_PORT" in str(exc.value) and "PRIVATE" not in str(exc.value)


def test_namespace_is_portable_between_windows_writer_and_local_dashboard():
    assert namespace_for(r"C:\Users\User\pennyquant\data\system.db") == "system"
    assert namespace_for("/workspace/project/data/system.db") == "system"
    assert namespace_for("data/system.db", "System-Paper") == "system-paper"
    assert namespace_for("data/한글.db") == namespace_for("other/한글.db")
    assert namespace_for("data/one.two.db") != namespace_for("data/one_two.db")
    assert len(namespace_for("x" * 100 + ".db")) <= 64
    with pytest.raises(ValueError, match="environment"):
        namespace_for("mysql://user:PRIVATE@localhost/pennyquant")
