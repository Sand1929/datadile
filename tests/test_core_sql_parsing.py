import pytest

from datadile.core import _validate_read_only_query


@pytest.mark.parametrize(
    "query",
    [
        "select 1",
        "with rows as (select 1 as value) select value from rows",
        "show timezone",
        "explain select 1",
        "values (1)",
        "-- comment before query\nselect 1",
    ],
)
def test_validate_read_only_query_allows_read_statements(query):
    """Read-only statements pass SQL parsing validation."""
    _validate_read_only_query(query)


@pytest.mark.parametrize(
    "query, message",
    [
        ("", "must contain"),
        ("insert into users values (1)", "start with"),
        ("update users set name = 'x'", "start with"),
        ("delete from users", "start with"),
        ("drop table users", "start with"),
        ("select 1; delete from users", "exactly one"),
        ("with deleted as (delete from users returning id) select * from deleted", "DELETE"),
        ("set search_path to public", "start with"),
    ],
)
def test_validate_read_only_query_rejects_write_or_control_statements(query, message):
    """Write, control, and multi-statement SQL fail validation."""
    with pytest.raises(ValueError, match=message):
        _validate_read_only_query(query)
