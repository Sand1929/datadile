---
name: datadile
description: Use when creating, editing, reviewing, or debugging Datadile data tests, datadile.yaml config, *.dile.yaml files, SQL expectations, or the datadile CLI/package code. Load this skill before changing data test YAML, connection configuration, test discovery, expectation parsing, result normalization, or CLI behavior.
---

# Datadile

Datadile runs YAML-defined data tests against query results. Use this skill when work involves Datadile test files, configuration, CLI behavior, or package internals.

## Use Cases

- Add or update `*.dile.yaml` files near application code.
- Review data tests for safe SQL, clear expectations, and valid severities.
- Create or edit `datadile.yaml` without storing secrets in the file.
- Debug `datadile test` failures, test discovery, config lookup, or data source selection.
- Modify Datadile internals such as parsing, expectation evaluation, result normalization, or CLI commands.

## Core Rules

- Datadile discovers only files named `*.dile.yaml` when `datadile test` is run without a path.
- Each test needs `name`, `description`, `query`, and `expect`.
- `severity` is optional and must be `LOW`, `MEDIUM`, or `HIGH`; it defaults to `MEDIUM`.
- `data_source` is optional; without it, Datadile uses `default_data_source` from `datadile.yaml`.
- Keep passwords and API keys in environment variables. Use `password_env` and `api_key_env`; do not put secret values directly in YAML.
- PostgreSQL is the local execution target today. Keep test YAML generic enough that other query engines can be added later.

## Data Test Examples

Use this shape for count checks:

```yaml
tests:
  - name: no_failed_orders_today
    description: Orders should not fail during the current day.
    severity: HIGH
    data_source: app_db
    query: |
      select count(*) as failed_orders
      from orders
      where status = 'failed'
        and created_at >= current_date
    expect: "= 0"
```

Use list expectations for one-column multi-row checks:

```yaml
tests:
  - name: active_plan_ids_are_known
    description: Active subscriptions should only use known plan IDs.
    query: |
      select distinct plan_id
      from subscriptions
      where status = 'active'
      order by plan_id
    expect: "= [1, 2, 3]"
```

## Config Examples

Local config lives at `./datadile.yaml`; user config lives at `~/.datadile/datadile.yaml`. Local config takes precedence.

```yaml
api_key_env: DATADILE_API_KEY
default_data_source: main

data_sources:
  main:
    type: postgresql
    host: localhost
    port: 5432
    user: myuser
    database: mydb
    password_env: DATADILE_DATA_SOURCE_PASSWORD
```

Server-backed data sources can reference an ID, but remote execution is not implemented in the local package yet:

```yaml
api_key_env: DATADILE_API_KEY
default_data_source: warehouse

data_sources:
  warehouse:
    id: ds_abc123
```

## Expectation Semantics

- Supported operators are `=`, `!=`, `>`, `>=`, `<`, and `<=`.
- One-row, one-column results compare as a scalar, such as `= 0`.
- Multi-row, one-column results compare as a list, such as `= [1, 2, 3]`.
- Wider rows compare as dictionaries or lists of dictionaries.
- Empty result sets normalize to `None`, so use `= null` when that is intentional.

## Coding Guidance

- Preserve the `*.dile.yaml` discovery rule unless the user explicitly asks to broaden it.
- Do not add support for inline passwords or API keys.
- Keep YAML examples quoted around expectation strings, especially values beginning with comparison operators.
- When changing CLI behavior, keep `datadile test [filepath]` working.
- When changing config behavior, keep local `datadile.yaml` precedence over `~/.datadile/datadile.yaml`.
