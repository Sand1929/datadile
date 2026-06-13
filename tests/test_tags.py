import json
import yaml
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest

from datadile.core import (
    load_data_tests,
    _parse_data_test,
    test_command as run_test_command,
    write_results_file,
    _cloud_run_payload,
    DataTest,
    DataTestResult,
)


def test_load_data_tests_with_tags(tmp_path):
    """Test loading data tests that have tags defined at the individual test level."""
    test_file = tmp_path / "test_tags.dile.yaml"
    test_file.write_text(
        yaml.safe_dump(
            {
                "tests": [
                    {
                        "name": "test_one",
                        "description": "First test",
                        "query": "select 1",
                        "expect": "= 1",
                        "tags": ["pre-flight", "migration"],
                    },
                    {
                        "name": "test_two",
                        "description": "Second test",
                        "query": "select 2",
                        "expect": "= 2",
                        "tags": "periodic",  # string fallback
                    },
                    {
                        "name": "test_three",
                        "description": "Third test",
                        "query": "select 3",
                        "expect": "= 3",
                        # no tags
                    },
                ]
            }
        )
    )

    tests = load_data_tests(test_file)
    assert len(tests) == 3
    assert tests[0].tags == ["pre-flight", "migration"]
    assert tests[1].tags == ["periodic"]
    assert tests[2].tags == []


def test_load_data_tests_with_defaults(tmp_path):
    """Test that defaults are applied to tests and tags are merged correctly."""
    test_file = tmp_path / "test_defaults.dile.yaml"
    test_file.write_text(
        yaml.safe_dump(
            {
                "defaults": {
                    "tags": ["common", "migration"],
                    "severity": "HIGH",
                    "data_source": "warehouse",
                },
                "tests": [
                    {
                        "name": "test_one",
                        "description": "First test",
                        "query": "select 1",
                        "expect": "= 1",
                        "tags": ["pre-flight"],  # Should be merged with defaults
                    },
                    {
                        "name": "test_two",
                        "description": "Second test",
                        "query": "select 2",
                        "expect": "= 2",
                        # Should inherit defaults, including tags
                    },
                    {
                        "name": "test_three",
                        "description": "Third test",
                        "query": "select 3",
                        "expect": "= 3",
                        "severity": "LOW",  # Overrides default severity
                    },
                ]
            }
        )
    )

    tests = load_data_tests(test_file)
    assert len(tests) == 3

    # Merged tags
    assert tests[0].tags == ["common", "migration", "pre-flight"]
    assert tests[0].severity == "HIGH"
    assert tests[0].data_source == "warehouse"

    # Inherited defaults
    assert tests[1].tags == ["common", "migration"]
    assert tests[1].severity == "HIGH"
    assert tests[1].data_source == "warehouse"

    # Overridden field
    assert tests[2].tags == ["common", "migration"]
    assert tests[2].severity == "LOW"


def test_test_command_filtering_by_tags(monkeypatch, tmp_path):
    """Test that test_command correctly filters loaded tests using the tags CLI flags."""
    monkeypatch.chdir(tmp_path)

    (tmp_path / "datadile.yaml").write_text(
        yaml.safe_dump(
            {
                "default_data_source": "main",
                "data_sources": {
                    "main": {
                        "type": "postgresql",
                        "host": "localhost",
                        "port": 5432,
                        "user": "myuser",
                        "database": "mydb",
                    }
                },
            }
        )
    )

    test_file = tmp_path / "my_tests.dile.yaml"
    test_file.write_text(
        yaml.safe_dump(
            [
                {
                    "name": "pre_flight_test",
                    "description": "Pre-flight check",
                    "query": "select 1",
                    "expect": "= 1",
                    "tags": ["pre-flight"],
                },
                {
                    "name": "post_run_test",
                    "description": "Post-run check",
                    "query": "select 1",
                    "expect": "= 1",
                    "tags": ["post-run", "migration"],
                },
                {
                    "name": "ad_hoc_test",
                    "description": "Ad-hoc check",
                    "query": "select 1",
                    "expect": "= 1",
                    "tags": ["ad-hoc"],
                },
            ]
        )
    )

    # Mock dependencies of test_command
    run_tests_calls = []

    def mock_run_data_tests(tests, data_source_config):
        run_tests_calls.append(tests)
        return [DataTestResult(test=t, passed=True) for t in tests]

    monkeypatch.setattr("datadile.core.run_data_tests", mock_run_data_tests)
    monkeypatch.setattr("datadile.core.print_results", lambda results: None)
    monkeypatch.setattr("datadile.core.record_data_test_runs", lambda results, dt: None)

    # 1. Test filtering with single tag
    args_preflight = SimpleNamespace(
        filepath=str(test_file),
        tags=["pre-flight"],
        results_file=None,
    )
    run_test_command(args_preflight)
    assert len(run_tests_calls[-1]) == 1
    assert run_tests_calls[-1][0].name == "pre_flight_test"

    # 2. Test filtering with multiple comma-separated tags or case-insensitive matching
    args_multi = SimpleNamespace(
        filepath=str(test_file),
        tags=["POST-RUN,MIGRATION"],
        results_file=None,
    )
    run_test_command(args_multi)
    assert len(run_tests_calls[-1]) == 1
    assert run_tests_calls[-1][0].name == "post_run_test"

    # 3. Test matching multiple distinct tags (acts as OR)
    args_or = SimpleNamespace(
        filepath=str(test_file),
        tags=["pre-flight", "ad-hoc"],
        results_file=None,
    )
    run_test_command(args_or)
    assert len(run_tests_calls[-1]) == 2
    names = {t.name for t in run_tests_calls[-1]}
    assert names == {"pre_flight_test", "ad_hoc_test"}


def test_write_results_file_includes_tags(tmp_path):
    """Test that tags are written out to the JSON results file."""
    test = DataTest(
        name="test_one",
        description="First test",
        query="select 1",
        expect="= 1",
        tags=["pre-flight", "critical"],
    )
    result = DataTestResult(test=test, passed=True, row_count=1)

    results_file = tmp_path / "results.json"
    write_results_file([result], results_file)

    payload = json.loads(results_file.read_text())
    assert len(payload) == 1
    assert payload[0]["name"] == "test_one"
    assert payload[0]["tags"] == ["pre-flight", "critical"]


def test_cloud_run_payload_includes_tags():
    """Test that tags are correctly formatted in the cloud run payload."""
    test = DataTest(
        name="test_one",
        description="First test",
        query="select 1",
        expect="= 1",
        tags=["pre-flight", "cloud-sync"],
        filepath="foo/bar.dile.yaml",
    )
    result = DataTestResult(test=test, passed=True, row_count=1)

    payload = _cloud_run_payload(result, datetime.now(timezone.utc))
    assert payload["name"] == "test_one"
    assert payload["tags"] == ["pre-flight", "cloud-sync"]
