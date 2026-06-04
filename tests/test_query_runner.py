import json
from datetime import datetime, timezone

import pytest

from datadile import core
from datadile.core import (
    DataTest,
    DataTestResult,
    QueryExecutionResult,
    _build_query_runner,
    _cloud_run_payload,
    run_data_tests,
    write_results_file,
)


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


def test_build_query_runner_limits_rows_but_counts_full_result(monkeypatch):
    """PostgreSQL runners retain a bounded sample while counting every result row."""

    class FakeRow:
        def __init__(self, mapping):
            self._mapping = mapping

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return None

        def execute(self, statement):
            if str(statement) == "SELECT 1":
                return []
            return [FakeRow({"id": value}) for value in range(150)]

    class FakeEngine:
        def execution_options(self, **options):
            return self

        def connect(self):
            return FakeConnection()

    monkeypatch.setattr(core, "create_engine", lambda url, **kwargs: FakeEngine())

    run_query = _build_query_runner(
        {
            "type": "postgresql",
            "user": "user",
            "password": "password",
            "host": "localhost",
            "port": 5432,
            "database": "datadile",
        }
    )

    result = run_query("select id from orders")

    assert result.row_count == 150
    assert len(result.rows) == 100
    assert result.rows[0] == {"id": 0}
    assert result.rows[-1] == {"id": 99}


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


def test_run_data_tests_supports_row_count_expectations(monkeypatch):
    """row_count expectations compare cardinality while preserving inspectable rows."""

    def fake_build_query_runner(data_source_config):
        return lambda query: QueryExecutionResult(
            rows=[
                {"id": 1, "status": "failed"},
                {"id": 2, "status": "failed"},
            ],
            row_count=2,
        )

    monkeypatch.setattr(core, "_build_query_runner", fake_build_query_runner)

    results = run_data_tests(
        [
            DataTest(
                name="failed orders exist",
                description="Failed orders should be inspectable.",
                query="select id, status from orders where status = 'failed'",
                expect="row_count > 1",
            )
        ],
        {"type": "postgresql"},
    )

    assert results[0].passed is True
    assert results[0].row_count == 2
    assert results[0].actual == [
        {"id": 1, "status": "failed"},
        {"id": 2, "status": "failed"},
    ]


def test_run_data_tests_supports_zero_row_count_expectations(monkeypatch):
    """row_count can assert that a row-returning query found no rows."""

    monkeypatch.setattr(
        core,
        "_build_query_runner",
        lambda data_source_config: lambda query: QueryExecutionResult(rows=[], row_count=0),
    )

    results = run_data_tests(
        [
            DataTest(
                name="no failed orders",
                description="There should be no failed orders.",
                query="select id, status from orders where status = 'failed'",
                expect="row_count = 0",
            )
        ],
        {"type": "postgresql"},
    )

    assert results[0].passed is True
    assert results[0].row_count == 0
    assert results[0].actual is None


def test_cloud_run_payload_preserves_row_count_expectation_subject(monkeypatch):
    """Cloud uploads distinguish row_count assertions from scalar result assertions."""
    monkeypatch.setattr(core, "_cloud_filepath", lambda path: "orders.dile.yaml")
    finished_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

    payload = _cloud_run_payload(
        DataTestResult(
            test=DataTest(
                name="failed_orders_are_limited",
                description="Failed orders should be limited.",
                query="select id from orders where status = 'failed'",
                expect="row_count <= 1",
                filepath="orders.dile.yaml",
            ),
            actual=[{"id": 1}, {"id": 2}],
            row_count=2,
            passed=False,
        ),
        finished_at,
    )

    assert payload["expected_subject"] == "row_count"
    assert payload["expected_operator"] == "lte"
    assert payload["expected_value"] == 1
    assert payload["actual_row_count"] == 2
    assert payload["actual_value"] == [{"id": 1}, {"id": 2}]


def test_write_results_file_preserves_full_result_details(tmp_path):
    """The results file contains data that may be truncated in the console table."""
    destination = tmp_path / "nested" / "results.json"

    write_results_file(
        [
            DataTestResult(
                test=DataTest(
                    name="failed_orders_are_limited",
                    description="Failed orders should be limited.",
                    query="select id, status from orders where status = 'failed'",
                    expect="row_count <= 1",
                    severity="HIGH",
                    filepath="orders.dile.yaml",
                ),
                actual=[{"id": 1, "status": "failed"}, {"id": 2, "status": "failed"}],
                row_count=2,
                passed=False,
            ),
            DataTestResult(
                test=DataTest(
                    name="query_errors_are_recorded",
                    description="Errors should be inspectable.",
                    query="select * from missing_table",
                    expect="= 0",
                ),
                passed=False,
                error="relation does not exist",
            ),
        ],
        destination,
    )

    payload = json.loads(destination.read_text())

    assert payload[0] == {
        "status": "failed",
        "severity": "HIGH",
        "filepath": "orders.dile.yaml",
        "name": "failed_orders_are_limited",
        "description": "Failed orders should be limited.",
        "query": "select id, status from orders where status = 'failed'",
        "expect": "row_count <= 1",
        "actual": [{"id": 1, "status": "failed"}, {"id": 2, "status": "failed"}],
        "row_count": 2,
        "error": None,
    }
    assert payload[1]["status"] == "error"
    assert payload[1]["error"] == "relation does not exist"
