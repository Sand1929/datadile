import io
import json
from types import SimpleNamespace

from rich.console import Console

from datadile import context
from datadile.context import context_command, extract_query_references


def _capture_console(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(context, "console", Console(file=output, force_terminal=False, width=200))
    return output


def test_extract_query_references_resolves_tables_aliases_and_columns():
    """SQL context parsing uses sqlglot to resolve table aliases."""
    references = extract_query_references(
        """
        select o.status, c.id
        from public.orders as o
        join customers c on c.id = o.customer_id
        where o.created_at >= current_date
        """,
        "postgresql",
    )

    assert "orders" in references.tables
    assert "public.orders" in references.tables
    assert "customers" in references.tables
    assert "orders.status" in references.columns
    assert "public.orders.status" in references.columns
    assert "customers.id" in references.columns
    assert "orders.customer_id" in references.columns


def test_context_command_filters_local_tests_by_data_source_and_column(monkeypatch, tmp_path):
    """Local context only returns tests with same data source and overlapping references."""
    monkeypatch.chdir(tmp_path)
    output = _capture_console(monkeypatch)
    (tmp_path / "datadile.yaml").write_text(
        """
default_data_source: app_db
data_sources:
  app_db:
    type: postgresql
  warehouse:
    type: postgresql
"""
    )
    (tmp_path / "orders.dile.yaml").write_text(
        """
tests:
  - name: no_failed_orders
    identity: orders.no_failed_orders
    description: Orders should not be failed today.
    severity: HIGH
    data_source: app_db
    query: |
      select count(*)
      from orders
      where status = 'failed'
    expect: "= 0"
  - name: warehouse_orders_are_positive
    description: Warehouse orders should be positive.
    data_source: warehouse
    query: |
      select count(*)
      from orders
      where status = 'failed'
    expect: "= 0"
  - name: users_have_emails
    description: Users should have emails.
    data_source: app_db
    query: |
      select count(*)
      from users
      where email is null
    expect: "= 0"
"""
    )

    context_command(
        SimpleNamespace(data_source="app_db", table=[], column=["orders.status"], format="json", no_cloud=True)
    )

    payload = json.loads(output.getvalue())
    assert [test["identity"] for test in payload["tests"]] == ["orders.no_failed_orders"]
    assert payload["tests"][0]["source"] == "local"
    assert payload["tests"][0]["data_source"] == "app_db"


def test_context_command_fetches_cloud_context_when_api_key_exists(monkeypatch, tmp_path):
    """Cloud context is requested with data source and table/column filters."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATADILE_API_KEY", "secret")
    output = _capture_console(monkeypatch)
    captured = {}
    (tmp_path / "datadile.yaml").write_text(
        """
api_key_env: DATADILE_API_KEY
default_data_source: app_db
data_sources:
  app_db:
    type: postgresql
"""
    )

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "tests": [
                    {
                        "identity": "orders.no_cancelled_orders",
                        "name": "no_cancelled_orders",
                        "description": "Orders should not be cancelled.",
                        "data_source": "app_db",
                        "severity": "HIGH",
                        "query": "select count(*) from orders where status = 'cancelled'",
                        "expect": "= 0",
                        "references": {"tables": ["orders"], "columns": ["orders.status"]},
                        "last_status": "PASSED",
                    }
                ]
            }

    def fake_get(url, params, headers, timeout):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(context.httpx, "get", fake_get)

    context_command(
        SimpleNamespace(data_source="app_db", table=["orders"], column=["orders.status"], format="json", no_cloud=False)
    )

    payload = json.loads(output.getvalue())
    assert captured["url"] == "https://datadile.io/api/datatests/context/"
    assert captured["params"] == {"data_source": "app_db", "tables": "orders", "columns": "orders.status"}
    assert captured["headers"] == {"Authorization": "Token secret"}
    assert captured["timeout"] == 10
    assert [test["identity"] for test in payload["tests"]] == ["orders.no_cancelled_orders"]
    assert payload["tests"][0]["last_status"] == "PASSED"
