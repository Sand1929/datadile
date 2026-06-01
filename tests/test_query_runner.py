import pytest

from datadile import core
from datadile.core import DataTest, _build_query_runner, run_data_tests


def test_build_query_runner_checks_database_connection_with_timeout(monkeypatch):
    """PostgreSQL query runners fail fast by checking connectivity on creation."""
    captured = {}

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return None

        def execute(self, statement):
            captured["statement"] = str(statement)
            return []

    class FakeEngine:
        def execution_options(self, **options):
            captured["options"] = options
            return self

        def connect(self):
            captured["connected"] = True
            return FakeConnection()

    def fake_create_engine(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return FakeEngine()

    monkeypatch.setattr(core, "create_engine", fake_create_engine)

    _build_query_runner(
        {
            "type": "postgresql",
            "user": "user",
            "password": "password",
            "host": "localhost",
            "port": 5432,
            "database": "datadile",
        }
    )

    assert captured["kwargs"]["connect_args"] == {"connect_timeout": 10}
    assert captured["options"] == {"postgresql_readonly": True}
    assert captured["connected"] is True
    assert captured["statement"] == "SELECT 1"


def test_build_query_runner_raises_friendly_connection_error(monkeypatch):
    """Connection failures include data source details and setup guidance."""

    class FakeEngine:
        def execution_options(self, **options):
            return self

        def connect(self):
            raise OSError("host not found")

    monkeypatch.setattr(core, "create_engine", lambda url, **kwargs: FakeEngine())

    with pytest.raises(RuntimeError) as exc_info:
        _build_query_runner(
            {
                "type": "postgresql",
                "user": "user",
                "password": "password",
                "host": "db.example.com",
                "port": 5432,
                "database": "datadile",
            }
        )

    message = str(exc_info.value)
    assert "Could not connect to PostgreSQL database 'datadile'" in message
    assert "db.example.com:5432" in message
    assert "as user 'user'" in message
    assert "Check datadile.yaml" in message
    assert "Connection timeout is set to 10 seconds" in message
    assert "Original error: host not found" in message
    assert exc_info.value.__suppress_context__ is True
    assert exc_info.value.__cause__ is None


def test_run_data_tests_raises_when_database_connection_fails(monkeypatch):
    """Connection failures are raised before individual test execution begins."""

    def fake_build_query_runner(data_source_config):
        raise RuntimeError("cannot connect")

    monkeypatch.setattr(core, "_build_query_runner", fake_build_query_runner)

    with pytest.raises(RuntimeError, match="cannot connect"):
        run_data_tests(
            [
                DataTest(
                    name="count users",
                    description="User count is non-negative",
                    query="select count(*) from users",
                    expect=">= 0",
                )
            ],
            {"type": "postgresql"},
        )
