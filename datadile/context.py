import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any

import httpx
import sqlglot
from rich.console import Console
from sqlglot import exp

from .config import get_api_host, get_api_key, load_config
from .core import SERVER_EXPECT_OPERATORS, discover_data_test_files, load_data_tests

console = Console()

DATA_TEST_CONTEXT_ENDPOINT = "/api/datatests/context/"
CONTEXT_OUTPUT_FORMATS = {"markdown", "json"}
CLIENT_EXPECT_OPERATORS = {server: client for client, server in SERVER_EXPECT_OPERATORS.items()}


@dataclass(frozen=True)
class QueryReferences:
    tables: frozenset[str]
    columns: frozenset[str]


@dataclass(frozen=True)
class DataTestContextEntry:
    source: str
    name: str
    description: str
    query: str
    expect: str
    severity: str = "MEDIUM"
    data_source: str | None = None
    identity: str | None = None
    filepath: str | None = None
    references: QueryReferences = QueryReferences(frozenset(), frozenset())
    last_status: str | None = None
    last_finished_at: str | None = None


def _api_url(host: str, path: str) -> str:
    """Build an absolute API URL from a configured host and endpoint path."""
    base_url = host if host.startswith(("http://", "https://")) else f"https://{host}"
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _normalize_reference(value: str | None) -> str | None:
    """Normalize table and column references for overlap comparisons."""
    if not value:
        return None
    parts = [part.strip(' "`[]').lower() for part in str(value).split(".") if part.strip(' "`[]')]
    return ".".join(parts) if parts else None


def _sqlglot_dialect(data_source_type: str | None) -> str | None:
    """Map Datadile data source types to sqlglot dialect names."""
    if not data_source_type:
        return "postgres"
    data_source_type = data_source_type.lower()
    if data_source_type in {"postgres", "postgresql"}:
        return "postgres"
    return None


def _table_reference_names(table: exp.Table) -> set[str]:
    """Return normalized table names, including schema-qualified and unqualified forms."""
    name = _normalize_reference(table.name)
    db = _normalize_reference(str(table.args.get("db"))) if table.args.get("db") else None
    catalog = _normalize_reference(str(table.args.get("catalog"))) if table.args.get("catalog") else None

    if not name:
        return set()

    names = {name}
    if db:
        names.add(f"{db}.{name}")
    if catalog and db:
        names.add(f"{catalog}.{db}.{name}")
    return names


def extract_query_references(query: str, data_source_type: str | None = "postgresql") -> QueryReferences:
    """Parse a SQL query and return referenced tables and columns."""
    dialect = _sqlglot_dialect(data_source_type)
    if not dialect:
        return QueryReferences(frozenset(), frozenset())

    try:
        parsed = sqlglot.parse_one(query, read=dialect)
    except sqlglot.errors.SqlglotError:
        return QueryReferences(frozenset(), frozenset())

    table_refs: set[str] = set()
    alias_to_tables: dict[str, set[str]] = {}
    table_nodes = list(parsed.find_all(exp.Table))
    for table in table_nodes:
        names = _table_reference_names(table)
        table_refs.update(names)

        table_name = _normalize_reference(table.name)
        if table_name:
            alias_to_tables[table_name] = names

        alias = _normalize_reference(table.alias)
        if alias:
            alias_to_tables[alias] = names

    column_refs: set[str] = set()
    unqualified_table = next(iter(table_refs)) if len({name.rsplit(".", 1)[-1] for name in table_refs}) == 1 else None
    for column in parsed.find_all(exp.Column):
        column_name = _normalize_reference(column.name)
        if not column_name:
            continue

        table_name = _normalize_reference(column.table)
        if table_name and table_name in alias_to_tables:
            for referenced_table in alias_to_tables[table_name]:
                column_refs.add(f"{referenced_table}.{column_name}")
        elif table_name:
            column_refs.add(f"{table_name}.{column_name}")
        elif unqualified_table:
            column_refs.add(f"{unqualified_table}.{column_name}")
        else:
            column_refs.add(column_name)

    return QueryReferences(frozenset(table_refs), frozenset(column_refs))


def _data_source_type(data_source: str | None) -> str:
    """Return the configured data source type without resolving secrets."""
    config = load_config(required=False)
    data_sources = config.get("data_sources")
    if not isinstance(data_sources, dict):
        return "postgresql"

    data_source_name = data_source or config.get("default_data_source")
    if not data_source_name:
        return "postgresql"

    data_source_config = data_sources.get(str(data_source_name))
    if not isinstance(data_source_config, dict):
        return "postgresql"
    return str(data_source_config.get("type", "postgresql"))


def _default_data_source_name() -> str | None:
    """Return the configured default data source without requiring credentials."""
    config = load_config(required=False)
    default_data_source = config.get("default_data_source")
    return str(default_data_source) if default_data_source else None


def _local_context_entries() -> list[DataTestContextEntry]:
    """Load local data tests as context entries."""
    default_data_source = _default_data_source_name()
    entries = []
    for test_path in discover_data_test_files():
        for test in load_data_tests(test_path):
            data_source = test.data_source or default_data_source
            entries.append(
                DataTestContextEntry(
                    source="local",
                    name=test.name,
                    description=test.description,
                    query=test.query,
                    expect=test.expect,
                    severity=test.severity,
                    data_source=data_source,
                    identity=test.identity,
                    filepath=test.filepath,
                    references=extract_query_references(test.query, _data_source_type(data_source)),
                )
            )
    return entries


def _cloud_context_entries(data_source: str, tables: set[str], columns: set[str]) -> tuple[list[DataTestContextEntry], str | None]:
    """Fetch cloud context entries when an API key is configured."""
    try:
        api_key = get_api_key()
    except Exception as exc:
        return [], f"Could not fetch Datadile Cloud context: {exc}"

    if not api_key:
        return [], None

    params = {
        "data_source": data_source,
        "tables": ",".join(sorted(tables)),
        "columns": ",".join(sorted(columns)),
    }
    headers = {"Authorization": f"Token {api_key}"}

    try:
        response = httpx.get(_api_url(get_api_host(), DATA_TEST_CONTEXT_ENDPOINT), params=params, headers=headers, timeout=10)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return [], f"Could not fetch Datadile Cloud context: {exc}"

    raw_tests = payload.get("tests", payload) if isinstance(payload, dict) else payload
    if not isinstance(raw_tests, list):
        return [], "Datadile Cloud returned an invalid context response."

    return [_cloud_context_entry(raw) for raw in raw_tests if isinstance(raw, dict)], None


def _cloud_context_entry(raw: dict[str, Any]) -> DataTestContextEntry:
    """Convert one cloud context payload item into a local context entry."""
    expected_operator = raw.get("expected_operator")
    expected_value = raw.get("expected_value")
    if raw.get("expect"):
        expect = str(raw["expect"])
    elif expected_operator in CLIENT_EXPECT_OPERATORS:
        expect = f"{CLIENT_EXPECT_OPERATORS[str(expected_operator)]} {repr(expected_value)}"
    else:
        expect = ""

    references = raw.get("references") if isinstance(raw.get("references"), dict) else {}
    tables = raw.get("tables", references.get("tables", []))
    columns = raw.get("columns", references.get("columns", []))
    query = str(raw.get("query", ""))
    data_source = str(raw["data_source"]) if raw.get("data_source") else None

    parsed_references = extract_query_references(query, _data_source_type(data_source)) if query else QueryReferences(frozenset(), frozenset())
    return DataTestContextEntry(
        source="cloud",
        name=str(raw.get("name", "")),
        description=str(raw.get("description", "")),
        query=query,
        expect=expect,
        severity=str(raw.get("severity", "MEDIUM")).upper(),
        data_source=data_source,
        identity=str(raw["identity"]) if raw.get("identity") else None,
        filepath=str(raw["filepath"]) if raw.get("filepath") else None,
        references=QueryReferences(
            frozenset(_normalize_reference(table) for table in tables if _normalize_reference(table)) or parsed_references.tables,
            frozenset(_normalize_reference(column) for column in columns if _normalize_reference(column)) or parsed_references.columns,
        ),
        last_status=str(raw["last_status"]) if raw.get("last_status") else None,
        last_finished_at=str(raw["last_finished_at"]) if raw.get("last_finished_at") else None,
    )


def _entry_identity(entry: DataTestContextEntry) -> tuple[str, ...]:
    """Return a stable dedupe key for a context entry."""
    if entry.identity:
        return ("identity", entry.identity)
    return ("fallback", entry.data_source or "", entry.name, entry.query)


def _column_overlap(requested_columns: set[str], referenced_columns: frozenset[str]) -> set[str]:
    """Return exact and explicit unqualified column overlaps."""
    overlaps = requested_columns & set(referenced_columns)
    for requested_column in requested_columns:
        if "." in requested_column:
            continue
        overlaps.update(column for column in referenced_columns if column.rsplit(".", 1)[-1] == requested_column)
    return overlaps


def _context_score(entry: DataTestContextEntry, tables: set[str], columns: set[str]) -> int:
    """Score context relevance by table and column overlap only."""
    table_matches = tables & set(entry.references.tables)
    column_matches = _column_overlap(columns, entry.references.columns)
    if not table_matches and not column_matches:
        return 0

    severity_bonus = 2 if entry.severity == "HIGH" else 1 if entry.severity == "MEDIUM" else 0
    return len(column_matches) * 10 + len(table_matches) * 3 + severity_bonus


def _relevant_context_entries(
    entries: list[DataTestContextEntry], data_source: str, tables: set[str], columns: set[str]
) -> list[DataTestContextEntry]:
    """Filter and rank context entries by same data source and reference overlap."""
    scored = []
    for entry in entries:
        if entry.data_source != data_source:
            continue
        score = _context_score(entry, tables, columns)
        if score:
            scored.append((score, entry))
    return [entry for score, entry in sorted(scored, key=lambda item: (item[0], item[1].severity), reverse=True)]


def _build_context_entries(
    data_source: str, tables: set[str], columns: set[str], include_cloud: bool = True
) -> tuple[list[DataTestContextEntry], list[str]]:
    """Build deduped context entries from local files and Datadile Cloud."""
    warnings = []
    entries = _local_context_entries()
    if include_cloud:
        cloud_entries, warning = _cloud_context_entries(data_source, tables, columns)
        entries.extend(cloud_entries)
        if warning:
            warnings.append(warning)

    deduped = []
    seen = set()
    for entry in entries:
        key = _entry_identity(entry)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)

    return _relevant_context_entries(deduped, data_source, tables, columns), warnings


def _context_payload(
    entries: list[DataTestContextEntry], data_source: str, tables: set[str], columns: set[str], warnings: list[str]
) -> dict[str, Any]:
    """Return a JSON-serializable context payload."""
    return {
        "data_source": data_source,
        "interest": {"tables": sorted(tables), "columns": sorted(columns)},
        "warnings": warnings,
        "tests": [
            {
                "source": entry.source,
                "identity": entry.identity,
                "name": entry.name,
                "description": entry.description,
                "filepath": entry.filepath,
                "severity": entry.severity,
                "data_source": entry.data_source,
                "references": {
                    "tables": sorted(entry.references.tables),
                    "columns": sorted(entry.references.columns),
                },
                "expect": entry.expect,
                "query": entry.query,
                "last_status": entry.last_status,
                "last_finished_at": entry.last_finished_at,
            }
            for entry in entries
        ],
    }


def _render_context_markdown(payload: dict[str, Any]) -> str:
    """Render context payload as Markdown for coding agents."""
    lines = ["# Datadile Context", "", f"Data source: {payload['data_source']}", "", "## Interest"]
    lines.append(f"Tables: {', '.join(payload['interest']['tables']) or '(none)'}")
    lines.append(f"Columns: {', '.join(payload['interest']['columns']) or '(none)'}")

    if payload["warnings"]:
        lines.extend(["", "## Warnings"])
        lines.extend(f"- {warning}" for warning in payload["warnings"])

    lines.extend(["", "## Relevant Tests"])
    if not payload["tests"]:
        lines.append("No matching Datadile tests found for these tables or columns.")
        return "\n".join(lines)

    for test in payload["tests"]:
        title = test["identity"] or test["name"]
        lines.extend(["", f"### {title}"])
        lines.append(f"Severity: {test['severity']}")
        lines.append(f"Source: {test['source']}")
        if test["filepath"]:
            lines.append(f"File: {test['filepath']}")
        if test["last_status"]:
            lines.append(f"Last status: {test['last_status']}")
        lines.extend(["", "Assumption:", test["description"] or "(none)"])
        lines.extend(["", "References:"])
        lines.append(f"Tables: {', '.join(test['references']['tables']) or '(none)'}")
        lines.append(f"Columns: {', '.join(test['references']['columns']) or '(none)'}")
        if test["expect"]:
            lines.extend(["", "Expectation:", test["expect"]])
        if test["query"]:
            lines.extend(["", "Query:", "```sql", test["query"].strip(), "```"])

    return "\n".join(lines)


def _print_context(payload: dict[str, Any], output_format: str) -> None:
    """Print context payload in the requested format."""
    if output_format == "json":
        console.print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        console.print(_render_context_markdown(payload))


def context_command(args: argparse.Namespace) -> None:
    """Print Datadile test context for tables and columns of interest."""
    data_source = args.data_source or _default_data_source_name()
    if not data_source:
        console.print("Missing data source. Pass --data-source or set default_data_source in datadile.yaml.")
        sys.exit(1)

    tables = {_normalize_reference(table) for table in args.table}
    columns = {_normalize_reference(column) for column in args.column}
    tables = {table for table in tables if table}
    columns = {column for column in columns if column}
    if not tables and not columns:
        console.print("Pass at least one --table or --column so Datadile can find relevant context.")
        sys.exit(1)

    if args.format not in CONTEXT_OUTPUT_FORMATS:
        console.print(f"Unknown context format '{args.format}'. Use markdown or json.")
        sys.exit(1)

    entries, warnings = _build_context_entries(data_source, tables, columns, include_cloud=not args.no_cloud)
    payload = _context_payload(entries, data_source, tables, columns, warnings)
    _print_context(payload, args.format)
