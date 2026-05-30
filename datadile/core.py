import argparse
import ast
import operator
import shutil
import sys
from dataclasses import dataclass
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any, Callable

import yaml
from rich.console import Console
from rich.table import Table
from sqlalchemy import create_engine, text

from .config import get_data_source_config

console = Console()

DATA_TEST_FILE_PATTERN = "*.dile.yaml"
DEFAULT_SKILL_INSTALL_PATH = Path(".opencode") / "skills" / "datadile" / "SKILL.md"
SEVERITIES = {"LOW", "MEDIUM", "HIGH"}
EXPECT_OPERATORS = {
    ">=": operator.ge,
    "<=": operator.le,
    "!=": operator.ne,
    "=": operator.eq,
    ">": operator.gt,
    "<": operator.lt,
}


@dataclass(frozen=True)
class DataTest:
    name: str
    description: str
    query: str
    expect: str
    severity: str = "MEDIUM"
    data_source: str | None = None


@dataclass(frozen=True)
class DataTestResult:
    test: DataTest
    actual: Any = None
    passed: bool = False
    error: str | None = None


def _build_postgres_connection_url(data_source_config: dict) -> str:
    return (
        f"postgresql+psycopg2://{data_source_config['user']}:{data_source_config['password']}"
        f"@{data_source_config['host']}:{data_source_config['port']}/{data_source_config['database']}"
    )


def load_data_tests(path: str | Path) -> list[DataTest]:
    test_path = Path(path)
    with test_path.open() as f:
        raw = yaml.safe_load(f)

    if isinstance(raw, dict) and "tests" in raw:
        raw_tests = raw["tests"]
    else:
        raw_tests = raw

    if not isinstance(raw_tests, list):
        raise ValueError("Data test file must contain a list of tests or a top-level 'tests' list.")

    return [_parse_data_test(item, index) for index, item in enumerate(raw_tests, start=1)]


def discover_data_test_files(root: str | Path = ".") -> list[Path]:
    return sorted(Path(root).rglob(DATA_TEST_FILE_PATTERN))


def _parse_data_test(raw: Any, index: int) -> DataTest:
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
    )


def evaluate_expectation(actual: Any, expectation: str) -> bool:
    op_symbol, expected = _parse_expectation(expectation)
    return EXPECT_OPERATORS[op_symbol](actual, expected)


def _parse_expectation(expectation: str) -> tuple[str, Any]:
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
    try:
        return ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return yaml.safe_load(value)


def run_data_tests(tests: list[DataTest], data_source_config: dict | Callable[[str | None], dict]) -> list[DataTestResult]:
    query_runners: dict[str | None, Callable[[str], list[dict[str, Any]]]] = {}
    default_run_query = None if callable(data_source_config) else _build_query_runner(data_source_config)
    results = []

    for test in tests:
        try:
            if callable(data_source_config):
                if test.data_source not in query_runners:
                    query_runners[test.data_source] = _build_query_runner(data_source_config(test.data_source))
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
    if data_source_config.get("id"):
        data_source_id = data_source_config["id"]
        raise ValueError(
            f"Server-backed data source '{data_source_id}' is configured, but remote test execution "
            "is not implemented in this package yet."
        )

    data_source_type = str(data_source_config.get("type", "postgresql")).lower()
    if data_source_type in {"postgres", "postgresql"}:
        engine = create_engine(_build_postgres_connection_url(data_source_config))

        def run_postgres_query(query: str) -> list[dict[str, Any]]:
            with engine.connect() as conn:
                return [dict(row._mapping) for row in conn.execute(text(query))]

        return run_postgres_query

    raise ValueError(f"Unsupported data source type '{data_source_type}'.")


def _normalize_query_result(rows: list[dict[str, Any]]) -> Any:
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
    if isinstance(value, dict):
        return {key: _normalize_value(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_normalize_value(inner) for inner in value]
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    return value


def print_results(results: list[DataTestResult]) -> None:
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
    test_paths = [Path(args.filepath)] if args.filepath else discover_data_test_files()
    if not test_paths:
        console.print(f"No data test files found matching {DATA_TEST_FILE_PATTERN}")
        sys.exit(1)

    tests = []
    for test_path in test_paths:
        tests.extend(load_data_tests(test_path))

    results = run_data_tests(tests, get_data_source_config)
    print_results(results)

    if any(not result.passed for result in results):
        sys.exit(1)


def install_skill_command(args: argparse.Namespace) -> None:
    destination = Path(args.destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    with resources.path("datadile.skill", "SKILL.md") as source:
        shutil.copyfile(source, destination)

    console.print(f"Installed Datadile skill to {destination}")


def main() -> None:
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
