import argparse
import ast
import json
import operator
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any, Callable

import httpx
import sqlparse
import yaml
from rich import box
from rich.console import Console
from rich.prompt import Confirm
from rich.table import Table
from rich.text import Text

from .config import CONFIG_FILENAME, USER_CONFIG_PATH, get_api_host, get_api_key, get_data_source_config

console = Console()
create_engine = None
text = None

DATA_TEST_FILE_PATTERN = "*.dile.yaml"
DEFAULT_CONFIG_TEMPLATE_PATH = Path(CONFIG_FILENAME)
DEFAULT_SKILL_AGENT = "opencode"
DEFAULT_AGENTS_SKILL_INSTALL_PATH = Path(".agents") / "skills" / "datadile" / "SKILL.md"
GLOBAL_AGENTS_SKILL_INSTALL_PATH = Path("~") / ".agents" / "skills" / "datadile" / "SKILL.md"
AGENT_SKILL_INSTALL_PATHS = {
    "opencode": DEFAULT_AGENTS_SKILL_INSTALL_PATH,
    "claude": Path(".claude") / "skills" / "datadile" / "SKILL.md",
    "claude-code": Path(".claude") / "skills" / "datadile" / "SKILL.md",
    "cursor": DEFAULT_AGENTS_SKILL_INSTALL_PATH,
    "github-copilot": DEFAULT_AGENTS_SKILL_INSTALL_PATH,
    "copilot": DEFAULT_AGENTS_SKILL_INSTALL_PATH,
    "openai-codex": DEFAULT_AGENTS_SKILL_INSTALL_PATH,
    "codex": DEFAULT_AGENTS_SKILL_INSTALL_PATH,
    "vscode": DEFAULT_AGENTS_SKILL_INSTALL_PATH,
    "vs-code": DEFAULT_AGENTS_SKILL_INSTALL_PATH,
    "visual-studio-code": DEFAULT_AGENTS_SKILL_INSTALL_PATH,
    "snowflake-cortex": Path(".cortex") / "skills" / "datadile" / "SKILL.md",
    "cortex": Path(".cortex") / "skills" / "datadile" / "SKILL.md",
}
GLOBAL_AGENT_SKILL_INSTALL_PATHS = {
    "opencode": GLOBAL_AGENTS_SKILL_INSTALL_PATH,
    "claude": Path("~") / ".claude" / "skills" / "datadile" / "SKILL.md",
    "claude-code": Path("~") / ".claude" / "skills" / "datadile" / "SKILL.md",
    "cursor": GLOBAL_AGENTS_SKILL_INSTALL_PATH,
    "github-copilot": GLOBAL_AGENTS_SKILL_INSTALL_PATH,
    "copilot": GLOBAL_AGENTS_SKILL_INSTALL_PATH,
    "openai-codex": GLOBAL_AGENTS_SKILL_INSTALL_PATH,
    "codex": GLOBAL_AGENTS_SKILL_INSTALL_PATH,
    "vscode": GLOBAL_AGENTS_SKILL_INSTALL_PATH,
    "vs-code": GLOBAL_AGENTS_SKILL_INSTALL_PATH,
    "visual-studio-code": GLOBAL_AGENTS_SKILL_INSTALL_PATH,
    "snowflake-cortex": Path("~") / ".snowflake" / "cortex" / "skills" / "datadile" / "SKILL.md",
    "cortex": Path("~") / ".snowflake" / "cortex" / "skills" / "datadile" / "SKILL.md",
}
DEFAULT_SKILL_INSTALL_PATH = AGENT_SKILL_INSTALL_PATHS[DEFAULT_SKILL_AGENT]
DATA_TEST_RUNS_ENDPOINT = "/api/datatests/runs/"
DEFAULT_DATABASE_CONNECT_TIMEOUT_SECONDS = 10
DEFAULT_MONGODB_CONNECT_TIMEOUT_SECONDS = 10
DATA_TEST_RESULT_ROW_LIMIT = 100
SEVERITIES = {"LOW", "MEDIUM", "HIGH"}
SERVER_EXPECT_OPERATORS = {
    "=": "eq",
    "!=": "neq",
    "<": "lt",
    "<=": "lte",
    ">": "gt",
    ">=": "gte",
}
EXPECT_OPERATORS = {
    ">=": operator.ge,
    "<=": operator.le,
    "!=": operator.ne,
    "=": operator.eq,
    ">": operator.gt,
    "<": operator.lt,
}
READ_ONLY_STATEMENT_STARTS = {"SELECT", "WITH", "SHOW", "EXPLAIN", "VALUES"}
DEFAULT_EXPECTED_SUBJECT = "result"
EXPECTATION_SUBJECTS = {"row_count"}
WRITE_STATEMENT_KEYWORDS = {
    "ALTER",
    "CALL",
    "CLUSTER",
    "COMMENT",
    "COPY",
    "CREATE",
    "DELETE",
    "DO",
    "DROP",
    "EXECUTE",
    "GRANT",
    "INSERT",
    "LISTEN",
    "LOCK",
    "MERGE",
    "NOTIFY",
    "REFRESH",
    "REINDEX",
    "RESET",
    "REVOKE",
    "SET",
    "TRUNCATE",
    "UPDATE",
    "VACUUM",
}
CONFIG_TEMPLATE = """# Optional. Only required for premium server-backed features.
api_key_env: DATADILE_API_KEY

default_data_source: main

data_sources:
  main:
    type: postgresql
    host: localhost
    port: 5432
    user: myuser
    database: mydb
    password_env: DATABASE_PASSWORD

# MongoDB data source example:
# data_sources:
#   main:
#     type: mongodb
#     host: localhost
#     port: 27017
#     database: mydb
#     # Optional. Required when user is set.
#     user: myuser
#     password_env: MONGODB_PASSWORD

# Premium server-backed data source example:
# data_sources:
#   main:
#     id: ds_abc123
"""


@dataclass(frozen=True)
class DataTest:
    name: str
    description: str
    query: str
    expect: str
    severity: str = "MEDIUM"
    data_source: str | None = None
    identity: str | None = None
    filepath: str | None = None


@dataclass(frozen=True)
class DataTestResult:
    test: DataTest
    actual: Any = None
    row_count: int | None = None
    passed: bool = False
    error: str | None = None


@dataclass(frozen=True)
class QueryExecutionResult:
    rows: list[dict[str, Any]]
    row_count: int


@dataclass(frozen=True)
class ExpectedComparison:
    subject: str
    operator: str
    value: Any


def _build_postgres_connection_url(data_source_config: dict) -> str:
    """Build a SQLAlchemy PostgreSQL connection URL from data source config."""
    return (
        f"postgresql+psycopg2://{data_source_config['user']}:{data_source_config['password']}"
        f"@{data_source_config['host']}:{data_source_config['port']}/{data_source_config['database']}"
    )


def load_data_tests(path: str | Path) -> list[DataTest]:
    """Load data tests from a YAML file."""
    test_path = Path(path)
    with test_path.open() as f:
        raw = yaml.safe_load(f)

    if isinstance(raw, dict) and "tests" in raw:
        raw_tests = raw["tests"]
    else:
        raw_tests = raw

    if not isinstance(raw_tests, list):
        raise ValueError("Data test file must contain a list of tests or a top-level 'tests' list.")

    return [_parse_data_test(item, index, test_path) for index, item in enumerate(raw_tests, start=1)]


def discover_data_test_files(root: str | Path = ".") -> list[Path]:
    """Find data test files below a root directory."""
    return sorted(Path(root).rglob(DATA_TEST_FILE_PATTERN))


def _parse_data_test(raw: Any, index: int, test_path: Path | None = None) -> DataTest:
    """Validate and convert a raw YAML test entry into a DataTest."""
    if not isinstance(raw, dict):
        raise ValueError(f"Test #{index} must be a mapping.")

    missing = [field for field in ("name", "description", "query", "expect") if field not in raw]
    if missing:
        raise ValueError(f"Test #{index} is missing required field(s): {', '.join(missing)}.")

    severity = str(raw.get("severity", "MEDIUM")).upper()
    if severity not in SEVERITIES:
        raise ValueError(f"Test #{index} has invalid severity '{severity}'. Use LOW, MEDIUM, or HIGH.")

    return DataTest(
        name=str(raw["name"]),
        description=str(raw["description"]),
        query=str(raw["query"]),
        expect=str(raw["expect"]),
        severity=severity,
        data_source=str(raw["data_source"]) if raw.get("data_source") else None,
        identity=str(raw["identity"]) if raw.get("identity") else None,
        filepath=str(test_path) if test_path else None,
    )


def evaluate_expectation(actual: Any, expectation: str, subjects: dict[str, Any] | None = None) -> bool:
    """Compare an actual value against a textual expectation expression."""
    comparison = _parse_expectation(expectation)
    actual_value = _comparison_actual_value(comparison, actual, subjects or {})
    return EXPECT_OPERATORS[comparison.operator](actual_value, comparison.value)


def _comparison_actual_value(comparison: ExpectedComparison, actual: Any, subjects: dict[str, Any]) -> Any:
    """Return the actual value for the expectation subject."""
    if comparison.subject == DEFAULT_EXPECTED_SUBJECT:
        return actual
    if comparison.subject not in subjects:
        raise ValueError(f"Expectation subject '{comparison.subject}' is not available for this result.")
    return subjects[comparison.subject]


def _parse_expectation(expectation: str) -> ExpectedComparison:
    """Parse an expectation into a subject, operator symbol, and expected value."""
    expression = expectation.strip()
    subject = DEFAULT_EXPECTED_SUBJECT
    for candidate in sorted(EXPECTATION_SUBJECTS):
        if expression == candidate or expression.startswith(f"{candidate} "):
            subject = candidate
            expression = expression[len(candidate) :].strip()
            break

    for op_symbol in sorted(EXPECT_OPERATORS, key=len, reverse=True):
        if expression.startswith(op_symbol):
            rhs = expression[len(op_symbol) :].strip()
            if not rhs:
                raise ValueError(f"Expectation '{expectation}' is missing a comparison value.")
            return ExpectedComparison(subject, op_symbol, _parse_expected_value(rhs))
    raise ValueError(
        f"Expectation '{expectation}' must start with one of: "
        f"{', '.join(sorted(EXPECT_OPERATORS, key=len, reverse=True))}."
    )


def _parse_expected_value(value: str) -> Any:
    """Parse an expectation value as a Python literal or YAML scalar."""
    try:
        return ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return yaml.safe_load(value)


def run_data_tests(tests: list[DataTest], data_source_config: dict | Callable[[str | None], dict]) -> list[DataTestResult]:
    """Execute data tests and return pass/fail results."""
    query_runners: dict[str | None, Callable[[str], QueryExecutionResult]] = {}
    if callable(data_source_config):
        for data_source in dict.fromkeys(test.data_source for test in tests):
            query_runners[data_source] = _build_query_runner(data_source_config(data_source))
        default_run_query = None
    else:
        default_run_query = _build_query_runner(data_source_config)
    results = []

    for test in tests:
        try:
            if callable(data_source_config):
                run_query = query_runners[test.data_source]
            else:
                run_query = default_run_query

            query_result = run_query(test.query)
            actual = _normalize_query_result(query_result.rows)
            row_count = query_result.row_count
            passed = evaluate_expectation(actual, test.expect, subjects={"row_count": row_count})
            results.append(DataTestResult(test=test, actual=actual, row_count=row_count, passed=passed))
        except Exception as exc:
            results.append(DataTestResult(test=test, passed=False, error=str(exc)))

    return results


def _build_query_runner(data_source_config: dict) -> Callable[[str], QueryExecutionResult]:
    """Build a read-only query runner for a configured data source."""
    if data_source_config.get("id"):
        data_source_id = data_source_config["id"]
        raise ValueError(
            f"Server-backed data source '{data_source_id}' is configured, but remote test execution "
            "is not implemented in this package yet."
        )

    data_source_type = str(data_source_config.get("type", "postgresql")).lower()
    if data_source_type in {"postgres", "postgresql"}:
        return _build_postgres_query_runner(data_source_config)

    if data_source_type in {"mongo", "mongodb"}:
        return _build_mongodb_query_runner(data_source_config)

    raise ValueError(f"Unsupported data source type '{data_source_type}'.")


def _build_postgres_query_runner(data_source_config: dict) -> Callable[[str], QueryExecutionResult]:
    sqlalchemy_create_engine, sqlalchemy_text = _load_postgres_dependencies()
    try:
        engine = sqlalchemy_create_engine(
            _build_postgres_connection_url(data_source_config),
            connect_args={"connect_timeout": DEFAULT_DATABASE_CONNECT_TIMEOUT_SECONDS},
        ).execution_options(
            postgresql_readonly=True,
        )
    except ModuleNotFoundError as exc:
        if exc.name == "psycopg2":
            raise RuntimeError(
                "PostgreSQL data sources require the PostgreSQL extra to be installed. "
                "Install it with: pip install 'datadile[postgres]'"
            ) from exc
        raise

    try:
        with engine.connect() as conn:
            conn.execute(sqlalchemy_text("SELECT 1"))
    except ModuleNotFoundError as exc:
        if exc.name == "psycopg2":
            raise RuntimeError(
                "PostgreSQL data sources require the PostgreSQL extra to be installed. "
                "Install it with: pip install 'datadile[postgres]'"
            ) from exc
        raise
    except Exception as exc:
        raise RuntimeError(
            "Could not connect to PostgreSQL database "
            f"'{data_source_config.get('database')}' at "
            f"{data_source_config.get('host')}:{data_source_config.get('port')} "
            f"as user '{data_source_config.get('user')}'. "
            f"Check datadile.yaml, your password environment variable, and network access. "
            f"Connection timeout is set to {DEFAULT_DATABASE_CONNECT_TIMEOUT_SECONDS} seconds. "
            f"Original error: {exc}"
        ) from None

    def run_postgres_query(query: str) -> QueryExecutionResult:
        """Run a validated read-only query against PostgreSQL."""
        _validate_read_only_query(query)
        with engine.connect() as conn:
            rows = []
            row_count = 0
            for row in conn.execute(sqlalchemy_text(query)):
                row_count += 1
                if len(rows) < DATA_TEST_RESULT_ROW_LIMIT:
                    rows.append(dict(row._mapping))
            return QueryExecutionResult(rows=rows, row_count=row_count)

    return run_postgres_query


def _load_postgres_dependencies() -> tuple[Callable[..., Any], Callable[[str], Any]]:
    global create_engine, text
    if create_engine is not None:
        return create_engine, text or (lambda statement: statement)

    try:
        from sqlalchemy import create_engine as sqlalchemy_create_engine
        from sqlalchemy import text as sqlalchemy_text
    except ImportError as exc:
        raise RuntimeError(
            "PostgreSQL data sources require the PostgreSQL extra to be installed. "
            "Install it with: pip install 'datadile[postgres]'"
        ) from exc

    create_engine = sqlalchemy_create_engine
    text = sqlalchemy_text
    return create_engine, text


def _build_mongodb_query_runner(data_source_config: dict) -> Callable[[str], QueryExecutionResult]:
    try:
        from pymongo import MongoClient
    except ImportError as exc:
        raise RuntimeError(
            "MongoDB data sources require the MongoDB extra to be installed. "
            "Install it with: pip install 'datadile[mongodb]'"
        ) from exc

    client_kwargs: dict[str, Any] = {"serverSelectionTimeoutMS": DEFAULT_MONGODB_CONNECT_TIMEOUT_SECONDS * 1000}
    if data_source_config.get("uri"):
        client_args = [data_source_config["uri"]]
    else:
        client_args = []
        client_kwargs.update(
            {
                "host": data_source_config.get("host", "localhost"),
                "port": int(data_source_config.get("port", 27017)),
            }
        )
        if data_source_config.get("user"):
            client_kwargs["username"] = data_source_config["user"]
            client_kwargs["password"] = data_source_config["password"]
        if data_source_config.get("auth_source"):
            client_kwargs["authSource"] = data_source_config["auth_source"]

    client = MongoClient(*client_args, **client_kwargs)
    database_name = data_source_config.get("database")
    try:
        client.admin.command("ping")
    except Exception as exc:
        target = data_source_config.get("uri") or f"{data_source_config.get('host')}:{data_source_config.get('port')}"
        raise RuntimeError(
            "Could not connect to MongoDB database "
            f"'{database_name}' at {target}. "
            f"Check datadile.yaml, your MongoDB environment variables, and network access. "
            f"Connection timeout is set to {DEFAULT_MONGODB_CONNECT_TIMEOUT_SECONDS} seconds. "
            f"Original error: {exc}"
        ) from None

    database = client[database_name]

    def run_mongodb_query(query: str) -> QueryExecutionResult:
        spec = _parse_mongodb_query(query)
        collection = database[spec["collection"]]

        if "pipeline" in spec:
            cursor = collection.aggregate(spec["pipeline"])
            return _mongodb_cursor_result(cursor)

        filter_spec = spec.get("filter", {})
        cursor = collection.find(filter_spec, spec.get("projection"))
        if "sort" in spec:
            cursor = cursor.sort(_mongodb_sort_spec(spec["sort"]))
        if "skip" in spec:
            cursor = cursor.skip(spec["skip"])
        if "limit" in spec:
            cursor = cursor.limit(spec["limit"])

        row_count = max(collection.count_documents(filter_spec) - spec.get("skip", 0), 0)
        if "limit" in spec:
            row_count = min(row_count, spec["limit"])
        if row_count == 0:
            return QueryExecutionResult(rows=[], row_count=0)

        rows = []
        for row in cursor.limit(min(row_count, DATA_TEST_RESULT_ROW_LIMIT)):
            rows.append(dict(row))
        return QueryExecutionResult(rows=rows, row_count=row_count)

    return run_mongodb_query


def _parse_mongodb_query(query: str) -> dict[str, Any]:
    spec = yaml.safe_load(query)
    if not isinstance(spec, dict):
        raise ValueError("MongoDB query must be a YAML or JSON mapping.")

    allowed_fields = {"collection", "filter", "projection", "sort", "skip", "limit", "pipeline"}
    unknown_fields = sorted(set(spec) - allowed_fields)
    if unknown_fields:
        raise ValueError(f"MongoDB query has unsupported field(s): {', '.join(unknown_fields)}.")

    collection = spec.get("collection")
    if not isinstance(collection, str) or not collection:
        raise ValueError("MongoDB query must include a collection name.")

    if "pipeline" in spec:
        if not isinstance(spec["pipeline"], list):
            raise ValueError("MongoDB pipeline must be a list of aggregation stages.")
        _validate_mongodb_pipeline_read_only(spec["pipeline"])
        return spec

    if "filter" in spec and not isinstance(spec["filter"], dict):
        raise ValueError("MongoDB filter must be a mapping.")
    if "skip" in spec and (not isinstance(spec["skip"], int) or spec["skip"] < 0):
        raise ValueError("MongoDB skip must be a non-negative integer.")
    if "limit" in spec and (not isinstance(spec["limit"], int) or spec["limit"] < 0):
        raise ValueError("MongoDB limit must be a non-negative integer.")
    return spec


def _validate_mongodb_pipeline_read_only(pipeline: list[Any]) -> None:
    for stage in pipeline:
        if not isinstance(stage, dict) or len(stage) != 1:
            raise ValueError("MongoDB pipeline stages must be single-key mappings.")
        stage_name = next(iter(stage))
        if stage_name in {"$out", "$merge"}:
            raise ValueError(f"MongoDB aggregation pipeline must be read-only; found disallowed stage '{stage_name}'.")


def _mongodb_sort_spec(sort: Any) -> Any:
    if isinstance(sort, dict):
        return list(sort.items())
    if isinstance(sort, list):
        return sort
    raise ValueError("MongoDB sort must be a mapping or list.")


def _mongodb_cursor_result(cursor: Any) -> QueryExecutionResult:
    rows = []
    row_count = 0
    for row in cursor:
        row_count += 1
        if len(rows) < DATA_TEST_RESULT_ROW_LIMIT:
            rows.append(dict(row))
    return QueryExecutionResult(rows=rows, row_count=row_count)


def _validate_read_only_query(query: str) -> None:
    """Raise ValueError when a SQL query is not read-only."""
    statements = [statement for statement in sqlparse.parse(query) if str(statement).strip()]
    if not statements:
        raise ValueError("Query must contain a read-only SQL statement.")
    if len(statements) > 1:
        raise ValueError("Query must contain exactly one read-only SQL statement.")

    statement = statements[0]
    first_keyword = _first_significant_keyword(statement)
    if first_keyword not in READ_ONLY_STATEMENT_STARTS:
        raise ValueError("Query must be read-only and start with SELECT, WITH, SHOW, EXPLAIN, or VALUES.")

    write_keyword = _find_write_keyword(statement)
    if write_keyword:
        raise ValueError(f"Query must be read-only; found disallowed SQL keyword '{write_keyword}'.")


def _first_significant_keyword(statement: sqlparse.sql.Statement) -> str | None:
    """Return the first non-comment, non-whitespace SQL token keyword."""
    for token in statement.flatten():
        if token.is_whitespace or token.ttype in sqlparse.tokens.Comment:
            continue
        return token.normalized
    return None


def _find_write_keyword(statement: sqlparse.sql.Statement) -> str | None:
    """Return the first disallowed write/control keyword in a statement."""
    for token in statement.flatten():
        if token.is_whitespace or token.ttype in sqlparse.tokens.Comment:
            continue
        if token.normalized in WRITE_STATEMENT_KEYWORDS:
            return token.normalized
    return None


def _normalize_query_result(rows: list[dict[str, Any]]) -> Any:
    """Convert query rows into the scalar or collection used for expectations."""
    normalized_rows = [_normalize_value(row) for row in rows]
    if not normalized_rows:
        return None

    if len(normalized_rows) == 1:
        row = normalized_rows[0]
        values = list(row.values())
        return values[0] if len(values) == 1 else row

    if all(len(row) == 1 for row in normalized_rows):
        return [next(iter(row.values())) for row in normalized_rows]
    return normalized_rows


def _normalize_value(value: Any) -> Any:
    """Normalize query result values into comparison-friendly Python values."""
    if isinstance(value, dict):
        return {key: _normalize_value(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_normalize_value(inner) for inner in value]
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if hasattr(value, "to_decimal"):
        return _normalize_value(value.to_decimal())
    if value.__class__.__module__.startswith("bson"):
        return str(value)
    return value


def _json_safe(value: Any) -> Any:
    """Return a JSON-serializable copy of a value, stringifying unsupported types."""
    return json.loads(json.dumps(value, default=str))


def _api_url(host: str, path: str) -> str:
    """Build an absolute API URL from a configured host and endpoint path."""
    base_url = host if host.startswith(("http://", "https://")) else f"https://{host}"
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _git_output(args: list[str], cwd: Path | None = None) -> str | None:
    """Run a Git command and return stripped stdout, or None if Git cannot answer."""
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            check=True,
            cwd=cwd,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


def _repo_name_from_origin(origin_url: str) -> str:
    """Extract the repository name from a Git remote URL."""
    repo_name = origin_url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    if repo_name.endswith(".git"):
        repo_name = repo_name[:-4]
    return repo_name


def _cloud_filepath(path: str | None) -> str:
    """Return the Datadile Cloud filepath for a local data test file."""
    if not path:
        return ""

    test_path = Path(path).resolve()
    git_cwd = test_path.parent if test_path.exists() else None
    repo_root = _git_output(["rev-parse", "--show-toplevel"], cwd=git_cwd)
    origin_url = _git_output(["config", "--get", "remote.origin.url"], cwd=git_cwd)
    if not repo_root:
        return str(Path(path))

    try:
        relative_path = test_path.relative_to(Path(repo_root).resolve())
    except ValueError:
        relative_path = Path(path)

    if not origin_url:
        return relative_path.as_posix()

    return f"{_repo_name_from_origin(origin_url)}/{relative_path.as_posix()}"


def _cloud_run_payload(result: DataTestResult, finished_at: datetime) -> dict[str, Any]:
    """Convert a local data test result into the cloud run-create payload."""
    comparison = _parse_expectation(result.test.expect)
    payload = {
        "filepath": _cloud_filepath(result.test.filepath),
        "name": result.test.name,
        "description": result.test.description,
        "query": result.test.query,
        "expected_subject": comparison.subject,
        "expected_operator": SERVER_EXPECT_OPERATORS[comparison.operator],
        "expected_value": _json_safe(comparison.value),
        "severity": result.test.severity,
        "status": "PASSED" if result.passed else "ERROR" if result.error else "FAILED",
        "actual_value": _json_safe(result.actual),
        "error_message": result.error or "",
        "finished_at": finished_at.isoformat(),
    }
    if comparison.subject == "row_count":
        payload["actual_row_count"] = result.row_count
    if result.test.identity:
        payload["identity"] = result.test.identity
    return payload


def record_data_test_runs(results: list[DataTestResult], finished_at: datetime) -> None:
    """Record local data test results in Datadile Cloud when an API key is configured."""
    try:
        api_key = get_api_key()
    except Exception as exc:
        console.print(f"[yellow]Could not record data test runs in Datadile Cloud: {exc}[/yellow]")
        return

    if not api_key:
        return

    url = _api_url(get_api_host(), DATA_TEST_RUNS_ENDPOINT)
    headers = {"Authorization": f"Token {api_key}"}

    with httpx.Client(timeout=10) as client:
        for result in results:
            try:
                response = client.post(url, json=_cloud_run_payload(result, finished_at), headers=headers)
                response.raise_for_status()
            except Exception as exc:
                console.print(f"[yellow]Could not record data test run '{result.test.name}' in Datadile Cloud: {exc}[/yellow]")


def print_results(results: list[DataTestResult]) -> None:
    """Render data test results to the console."""
    table = Table(
        title="Datadile Data Tests",
        title_style="bold cyan",
        header_style="bold",
        box=box.ROUNDED,
        expand=True,
    )
    table.add_column("Status", justify="center", no_wrap=True)
    table.add_column("Severity", no_wrap=True)
    table.add_column("Name", style="bold")
    table.add_column("Expect", style="cyan")
    table.add_column("Actual / Error", overflow="fold")

    for result in results:
        status = "PASS" if result.passed else "ERROR" if result.error else "FAIL"
        status_style = "bold green" if result.passed else "bold magenta" if result.error else "bold red"
        severity_style = {"HIGH": "red", "MEDIUM": "yellow", "LOW": "green"}.get(result.test.severity, "")
        actual = result.error if result.error else _format_console_value(result.actual)
        table.add_row(
            Text(status, style=status_style),
            Text(result.test.severity, style=severity_style),
            result.test.name,
            result.test.expect,
            actual,
        )

    console.print(table)

    failures = [result for result in results if not result.passed]
    summary_style = "bold green" if not failures else "bold red"
    console.print(f"[{summary_style}]{len(results) - len(failures)} passed, {len(failures)} failed[/{summary_style}]")


def _format_console_value(value: Any) -> str:
    """Format actual values for a readable console table."""
    safe_value = _json_safe(value)
    if isinstance(safe_value, (dict, list)):
        return json.dumps(safe_value, indent=2)
    return json.dumps(safe_value)


def write_results_file(results: list[DataTestResult], path: str | Path) -> None:
    """Write complete data test results to a JSON file."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "status": "passed" if result.passed else "error" if result.error else "failed",
            "severity": result.test.severity,
            "filepath": result.test.filepath,
            "name": result.test.name,
            "description": result.test.description,
            "query": result.test.query,
            "expect": result.test.expect,
            "actual": _json_safe(result.actual),
            "row_count": result.row_count,
            "error": result.error,
        }
        for result in results
    ]
    destination.write_text(json.dumps(payload, indent=2) + "\n")


def test_command(args: argparse.Namespace) -> None:
    """Run the CLI data test command."""
    test_paths = [Path(args.filepath)] if args.filepath else discover_data_test_files()
    if not test_paths:
        console.print(f"No data test files found matching {DATA_TEST_FILE_PATTERN}")
        sys.exit(1)

    tests = []
    for test_path in test_paths:
        tests.extend(load_data_tests(test_path))

    results = run_data_tests(tests, get_data_source_config)
    finished_at = datetime.now(timezone.utc)
    print_results(results)
    if args.results_file:
        write_results_file(results, args.results_file)
        console.print(f"Wrote full results to {Path(args.results_file)}")
    record_data_test_runs(results, finished_at)

    if any(not result.passed for result in results):
        sys.exit(1)


def context_command(args: argparse.Namespace) -> None:
    """Delegate context handling to the context module."""
    from .context import context_command as run_context_command

    run_context_command(args)


def install_skill_command(args: argparse.Namespace) -> None:
    """Install the bundled Datadile coding-agent skill file."""
    default_destination = GLOBAL_AGENT_SKILL_INSTALL_PATHS[args.agent] if args.global_install else AGENT_SKILL_INSTALL_PATHS[args.agent]
    destination = Path(args.destination or default_destination).expanduser()
    destination = destination if destination.is_absolute() else Path.cwd() / destination

    if not args.yes and not Confirm.ask(f"Install Datadile skill to {destination}?", default=False):
        console.print("Installation cancelled.")
        return

    destination.parent.mkdir(parents=True, exist_ok=True)

    with resources.path("datadile.skill", "SKILL.md") as source:
        shutil.copyfile(source, destination)

    console.print(f"Installed Datadile skill to {destination}")


def init_command(args: argparse.Namespace) -> None:
    """Write a starter Datadile config file."""
    if getattr(args, "global_install", False) and args.destination:
        console.print("Pass either a destination or --global/--user, not both.")
        sys.exit(1)

    default_destination = USER_CONFIG_PATH if getattr(args, "global_install", False) else DEFAULT_CONFIG_TEMPLATE_PATH
    destination = Path(args.destination or default_destination).expanduser()
    destination = destination if destination.is_absolute() else Path.cwd() / destination

    if destination.exists() and not args.force:
        console.print(f"Config file already exists at {destination}. Use --force to overwrite it.")
        sys.exit(1)

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(CONFIG_TEMPLATE)
    console.print(f"Wrote starter Datadile config to {destination}")


def main() -> None:
    """Parse CLI arguments and dispatch to the selected command."""
    parser = argparse.ArgumentParser(description="Datadile data quality CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    test_parser = subparsers.add_parser("test", help="Run YAML data tests")
    test_parser.add_argument("filepath", nargs="?", help="Path to a YAML data test file")
    test_parser.add_argument("--results-file", help="Write full test results to a JSON file")
    test_parser.set_defaults(func=test_command)

    context_parser = subparsers.add_parser("context", help="Show test context for tables and columns")
    context_parser.add_argument("--data-source", help="Data source name to search within")
    context_parser.add_argument(
        "--table",
        action="append",
        default=[],
        help="Table of interest. Repeat for multiple tables.",
    )
    context_parser.add_argument(
        "--column",
        action="append",
        default=[],
        help="Column of interest, preferably table-qualified. Repeat for multiple columns.",
    )
    context_parser.add_argument(
        "--format",
        choices=["json", "markdown"],
        default="markdown",
        help="Output format for coding agents or tools",
    )
    context_parser.add_argument("--no-cloud", action="store_true", help="Use only local *.dile.yaml files")
    context_parser.set_defaults(func=context_command)

    init_parser = subparsers.add_parser("init", help="Write a starter datadile.yaml config file")
    init_parser.add_argument(
        "destination",
        nargs="?",
        help="Where to write the config file (default: datadile.yaml)",
    )
    init_parser.add_argument(
        "--global",
        dest="global_install",
        action="store_true",
        help="Write to the user-level config at ~/.datadile/datadile.yaml",
    )
    init_parser.add_argument("--force", action="store_true", help="Overwrite the destination if it already exists")
    init_parser.set_defaults(func=init_command)

    skill_parser = subparsers.add_parser("install-skill", help="Install the bundled coding-agent skill")
    skill_parser.add_argument(
        "destination",
        nargs="?",
        help="Where to write the skill file (overrides --agent default)",
    )
    skill_parser.add_argument(
        "--agent",
        choices=sorted(AGENT_SKILL_INSTALL_PATHS),
        default=DEFAULT_SKILL_AGENT,
        help=f"Coding agent to install for (default: {DEFAULT_SKILL_AGENT})",
    )
    skill_parser.add_argument(
        "--global",
        dest="global_install",
        action="store_true",
        help="Install to the selected agent's user-level skills directory",
    )
    skill_parser.add_argument("-y", "--yes", action="store_true", help="Install without asking for confirmation")
    skill_parser.set_defaults(func=install_skill_command)

    args = parser.parse_args()
    args.func(args)
