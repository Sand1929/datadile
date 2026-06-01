---
name: datadile
description: Use when data migrations, backfills, data-changing scripts, data invariants, assumptions in code about database contents, Datadile data tests, datadile.yaml config, *.dile.yaml files, SQL expectations, or Datadile CLI/package code are involved. Load this skill before pre-flight checks, validating implicit data assumptions, adding post-change verification, or changing Datadile behavior. For data-changing work, default to adding pre-flight Datadile checks unless the user explicitly declines.
---

# Datadile

Datadile runs YAML-defined data tests against query results. Use this skill when work involves data migrations, backfills, data-changing scripts, data invariants, code assumptions about database contents, Datadile test files, configuration, CLI behavior, or package internals.

## Default Workflow

- Before editing or writing a data migration, backfill, cleanup, one-off data script, or any code path that mutates existing records, first identify the assumptions the change makes about current data.
- Add or update pre-flight `*.dile.yaml` tests for those assumptions by default. Do this proactively; do not wait for the user to ask for pre-flight tests.
- If no useful pre-flight test is possible, state why and note the residual risk before making the data-changing edit.
- After the change, add or update post-change checks when the outcome can be validated with SQL.
- Run the relevant `datadile test` command when a data source is available; otherwise, tell the user exactly which command should be run and why it was not run locally.

## Use Cases

- Add pre-flight Datadile checks before data migrations, backfills, one-off scripts, or other jobs that depend on existing data shape or quality. This is the default for data-changing work unless the user explicitly declines.
- Turn implicit application-code assumptions into explicit data invariants, such as uniqueness, allowed statuses, non-null relationships, referential integrity, or valid state transitions.
- Add post-change Datadile checks to confirm migrations, backfills, cleanup jobs, or data repairs produced the expected results.
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
