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
from rich.console import Console
from rich.table import Table
from sqlalchemy import create_engine, text

from .config import get_api_host, get_api_key, get_data_source_config

console = Console()

DATA_TEST_FILE_PATTERN = "*.dile.yaml"
DEFAULT_SKILL_INSTALL_PATH = Path(".opencode") / "skills" / "datadile" / "SKILL.md"
DATA_TEST_RUNS_ENDPOINT = "/api/datatests/runs/"
DEFAULT_DATABASE_CONNECT_TIMEOUT_SECONDS = 10
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
    passed: bool = False
    error: str | None = None


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


def evaluate_expectation(actual: Any, expectation: str) -> bool:
    """Compare an actual value against a textual expectation expression."""
    op_symbol, expected = _parse_expectation(expectation)
    return EXPECT_OPERATORS[op_symbol](actual, expected)


def _parse_expectation(expectation: str) -> tuple[str, Any]:
    """Parse an expectation into an operator symbol and expected value."""
    expression = expectation.strip()
    for op_symbol in sorted(EXPECT_OPERATORS, key=len, reverse=True):
        if expression.startswith(op_symbol):
            rhs = expression[len(op_symbol) :].strip()
            if not rhs:
                raise ValueError(f"Expectation '{expectation}' is missing a comparison value.")
            return op_symbol, _parse_expected_value(rhs)
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
    query_runners: dict[str | None, Callable[[str], list[dict[str, Any]]]] = {}
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

            rows = run_query(test.query)
            actual = _normalize_query_result(rows)
            passed = evaluate_expectation(actual, test.expect)
            results.append(DataTestResult(test=test, actual=actual, passed=passed))
        except Exception as exc:
            results.append(DataTestResult(test=test, passed=False, error=str(exc)))

    return results


def _build_query_runner(data_source_config: dict) -> Callable[[str], list[dict[str, Any]]]:
    """Build a read-only query runner for a configured data source."""
    if data_source_config.get("id"):
        data_source_id = data_source_config["id"]
        raise ValueError(
            f"Server-backed data source '{data_source_id}' is configured, but remote test execution "
            "is not implemented in this package yet."
        )

    data_source_type = str(data_source_config.get("type", "postgresql")).lower()
    if data_source_type in {"postgres", "postgresql"}:
        engine = create_engine(
            _build_postgres_connection_url(data_source_config),
            connect_args={"connect_timeout": DEFAULT_DATABASE_CONNECT_TIMEOUT_SECONDS},
        ).execution_options(
            postgresql_readonly=True,
        )

        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
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

        def run_postgres_query(query: str) -> list[dict[str, Any]]:
            """Run a validated read-only query against PostgreSQL."""
            _validate_read_only_query(query)
            with engine.connect() as conn:
                return [dict(row._mapping) for row in conn.execute(text(query))]

        return run_postgres_query

    raise ValueError(f"Unsupported data source type '{data_source_type}'.")


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
    expected_operator, expected_value = _parse_expectation(result.test.expect)
    payload = {
        "filepath": _cloud_filepath(result.test.filepath),
        "name": result.test.name,
        "description": result.test.description,
        "query": result.test.query,
        "expected_operator": SERVER_EXPECT_OPERATORS[expected_operator],
        "expected_value": _json_safe(expected_value),
        "severity": result.test.severity,
        "status": "PASSED" if result.passed else "ERROR" if result.error else "FAILED",
        "actual_value": _json_safe(result.actual),
        "error_message": result.error or "",
        "finished_at": finished_at.isoformat(),
    }
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
    table = Table(title="Datadile Data Tests")
    table.add_column("Status")
    table.add_column("Severity")
    table.add_column("Name")
    table.add_column("Expect")
    table.add_column("Actual")

    for result in results:
        status = "PASS" if result.passed else "FAIL"
        style = "green" if result.passed else "red"
        actual = result.error if result.error else repr(result.actual)
        table.add_row(
            f"[{style}]{status}[/{style}]",
            result.test.severity,
            result.test.name,
            result.test.expect,
            actual,
        )

    console.print(table)

    failures = [result for result in results if not result.passed]
    console.print(f"{len(results) - len(failures)} passed, {len(failures)} failed")


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
    record_data_test_runs(results, finished_at)

    if any(not result.passed for result in results):
        sys.exit(1)


def install_skill_command(args: argparse.Namespace) -> None:
    """Install the bundled Datadile coding-agent skill file."""
    destination = Path(args.destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    with resources.path("datadile.skill", "SKILL.md") as source:
        shutil.copyfile(source, destination)

    console.print(f"Installed Datadile skill to {destination}")


def main() -> None:
    """Parse CLI arguments and dispatch to the selected command."""
    parser = argparse.ArgumentParser(description="Datadile data quality CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    test_parser = subparsers.add_parser("test", help="Run YAML data tests")
    test_parser.add_argument("filepath", nargs="?", help="Path to a YAML data test file")
    test_parser.set_defaults(func=test_command)

    skill_parser = subparsers.add_parser("install-skill", help="Install the bundled coding-agent skill")
    skill_parser.add_argument(
        "destination",
        nargs="?",
        default=DEFAULT_SKILL_INSTALL_PATH,
        help=f"Where to write SKILL.md (default: {DEFAULT_SKILL_INSTALL_PATH})",
    )
    skill_parser.set_defaults(func=install_skill_command)

    args = parser.parse_args()
    args.func(args)
