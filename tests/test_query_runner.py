import json
import sys
from datetime import datetime, timezone
from io import StringIO
from types import SimpleNamespace

import pytest
from rich.console import Console

from datadile import core
from datadile.core import (
    DataTest,
    DataTestResult,
    QueryExecutionResult,
    _build_query_runner,
    _cloud_run_payload,
    print_results,
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


def test_build_query_runner_raises_friendly_postgres_extra_error(monkeypatch):
    """PostgreSQL runners explain how to install optional dependencies."""
    monkeypatch.setattr(core, "create_engine", None)

    def fake_import(name, *args, **kwargs):
        if name == "sqlalchemy":
            raise ImportError("No module named sqlalchemy")
        return original_import(name, *args, **kwargs)

    original_import = __import__
    monkeypatch.setattr("builtins.__import__", fake_import)

    with pytest.raises(RuntimeError) as exc_info:
        _build_query_runner({"type": "postgresql"})

    assert "pip install 'datadile[postgres]'" in str(exc_info.value)


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


def test_build_query_runner_runs_mongodb_find_queries(monkeypatch):
    """MongoDB runners execute YAML query documents through read-only find calls."""
    captured = {}
    documents = [{"_id": value, "status": "active"} for value in range(150)]

    class FakeCursor:
        def __init__(self, rows):
            self.rows = rows
            self.limit_value = None

        def sort(self, sort):
            captured["sort"] = sort
            return self

        def skip(self, skip):
            captured["skip"] = skip
            self.rows = self.rows[skip:]
            return self

        def limit(self, limit):
            self.limit_value = limit
            return self

        def __iter__(self):
            return iter(self.rows[: self.limit_value])

    class FakeCollection:
        def find(self, filter_spec, projection):
            captured["filter"] = filter_spec
            captured["projection"] = projection
            return FakeCursor(documents)

        def count_documents(self, filter_spec):
            captured["count_filter"] = filter_spec
            return len(documents)

    class FakeDatabase:
        def __getitem__(self, collection_name):
            captured["collection"] = collection_name
            return FakeCollection()

    class FakeAdmin:
        def command(self, command):
            captured["command"] = command

    class FakeMongoClient:
        def __init__(self, *args, **kwargs):
            captured["client_args"] = args
            captured["client_kwargs"] = kwargs
            self.admin = FakeAdmin()

        def __getitem__(self, database_name):
            captured["database"] = database_name
            return FakeDatabase()

    monkeypatch.setitem(sys.modules, "pymongo", SimpleNamespace(MongoClient=FakeMongoClient))

    run_query = _build_query_runner(
        {
            "type": "mongodb",
            "host": "localhost",
            "port": 27017,
            "database": "datadile",
        }
    )

    result = run_query(
        """
        collection: users
        filter:
          status: active
        projection:
          _id: 1
          status: 1
        sort:
          _id: 1
        skip: 10
        """
    )

    assert captured["client_args"] == ()
    assert captured["client_kwargs"] == {
        "serverSelectionTimeoutMS": 10000,
        "host": "localhost",
        "port": 27017,
    }
    assert captured["command"] == "ping"
    assert captured["database"] == "datadile"
    assert captured["collection"] == "users"
    assert captured["filter"] == {"status": "active"}
    assert captured["projection"] == {"_id": 1, "status": 1}
    assert captured["sort"] == [("_id", 1)]
    assert captured["skip"] == 10
    assert result.row_count == 140
    assert len(result.rows) == 100
    assert result.rows[0] == {"_id": 10, "status": "active"}
    assert result.rows[-1] == {"_id": 109, "status": "active"}


def test_build_query_runner_raises_friendly_mongodb_extra_error(monkeypatch):
    """MongoDB runners explain how to install optional dependencies."""
    monkeypatch.delitem(sys.modules, "pymongo", raising=False)

    def fake_import(name, *args, **kwargs):
        if name == "pymongo":
            raise ImportError("No module named pymongo")
        return original_import(name, *args, **kwargs)

    original_import = __import__
    monkeypatch.setattr("builtins.__import__", fake_import)

    with pytest.raises(RuntimeError) as exc_info:
        _build_query_runner({"type": "mongodb"})

    assert "pip install 'datadile[mongodb]'" in str(exc_info.value)


def test_build_query_runner_runs_mongodb_aggregation_queries(monkeypatch):
    """MongoDB runners support read-only aggregation pipelines."""
    captured = {}

    class FakeCollection:
        def aggregate(self, pipeline):
            captured["pipeline"] = pipeline
            return [{"status": "active", "count": 2}]

    class FakeDatabase:
        def __getitem__(self, collection_name):
            captured["collection"] = collection_name
            return FakeCollection()

    class FakeMongoClient:
        def __init__(self, *args, **kwargs):
            self.admin = SimpleNamespace(command=lambda command: None)

        def __getitem__(self, database_name):
            return FakeDatabase()

    monkeypatch.setitem(sys.modules, "pymongo", SimpleNamespace(MongoClient=FakeMongoClient))

    run_query = _build_query_runner({"type": "mongodb", "uri": "mongodb://localhost:27017", "database": "datadile"})

    result = run_query(
        """
        collection: users
        pipeline:
          - $match:
              status: active
          - $group:
              _id: "$status"
              count:
                $sum: 1
        """
    )

    assert captured["collection"] == "users"
    assert captured["pipeline"] == [
        {"$match": {"status": "active"}},
        {"$group": {"_id": "$status", "count": {"$sum": 1}}},
    ]
    assert result == QueryExecutionResult(rows=[{"status": "active", "count": 2}], row_count=1)


def test_mongodb_aggregation_rejects_write_stages(monkeypatch):
    """Aggregation pipelines cannot write through $out or $merge stages."""

    class FakeMongoClient:
        def __init__(self, *args, **kwargs):
            self.admin = SimpleNamespace(command=lambda command: None)

        def __getitem__(self, database_name):
            return SimpleNamespace()

    monkeypatch.setitem(sys.modules, "pymongo", SimpleNamespace(MongoClient=FakeMongoClient))

    run_query = _build_query_runner({"type": "mongodb", "uri": "mongodb://localhost:27017", "database": "datadile"})

    with pytest.raises(ValueError, match="disallowed stage '\$out'"):
        run_query(
            """
            collection: users
            pipeline:
              - $out: archived_users
            """
        )


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
        "tags": [],
    }
    assert payload[1]["status"] == "error"
    assert payload[1]["error"] == "relation does not exist"


def test_print_results_renders_readable_table(monkeypatch):
    output = StringIO()
    monkeypatch.setattr(core, "console", Console(file=output, force_terminal=False, width=120))

    print_results(
        [
            DataTestResult(
                test=DataTest(
                    name="failed_orders_are_limited",
                    description="Failed orders should be limited.",
                    query="select id, status from orders where status = 'failed'",
                    expect="row_count <= 1",
                    severity="HIGH",
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
        ]
    )

    rendered = output.getvalue()

    assert "Datadile Data Tests" in rendered
    assert "Actual / Error" in rendered
    assert "FAIL" in rendered
    assert "ERROR" in rendered
    assert '"status": "failed"' in rendered
    assert "relation does not exist" in rendered
    assert "0 passed, 2 failed" in rendered
