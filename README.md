# datadile

Datadile runs YAML-defined data tests against query results.

## Installation

```bash
pip install datadile
```

## Configuration

datadile looks for a YAML config file in two locations (local takes precedence):

1. `./datadile.yaml` (current directory)
2. `~/.datadile/datadile.yaml` (user-level)

Copy the example and fill in your values:

```bash
cp datadile.yaml.example datadile.yaml
```

```yaml
# Optional. Only required for premium server-backed features.
api_key_env: DATADILE_API_KEY

# Optional. Used by tests that do not set data_source.
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

Put data source passwords and API keys in environment variables, not in `datadile.yaml`:

```bash
export DATADILE_DATA_SOURCE_PASSWORD='your_password_here'
export DATADILE_API_KEY='your_api_key_here'
```

Use `password_env` or `api_key_env` if you want Datadile to read a different environment variable name.

Add more named entries under `data_sources` when tests need to run against multiple databases. `default_data_source` is optional, but tests that do not set `data_source` need a default.

`api_key_env` is optional. You only need it for premium features that access Datadile servers.

Data source entries can be defined inline, or referenced by ID if the connection details are stored on your Datadile account:

```yaml
api_key_env: DATADILE_API_KEY

default_data_source: warehouse

data_sources:
  warehouse:
    id: ds_abc123
```

`postgresql` is currently supported for local execution. Data tests keep `query` generic so query engines such as MongoDB can be added without changing the test format.

## Usage

```bash
datadile test
datadile test <path/to/file.dile.yaml>
```

With no path, Datadile recursively discovers only files matching `*.dile.yaml` from the current directory. Other YAML files, such as `docker-compose.yaml`, GitHub Actions workflows, Helm values, and OpenAPI specs, are ignored.

## Data Tests

Tests are defined in YAML. Each test has `name`, `description`, `query`, and `expect`. `severity` is optional and defaults to `MEDIUM`; valid values are `LOW`, `MEDIUM`, and `HIGH`. `data_source` is optional and references a named source from `data_sources`; otherwise Datadile uses `default_data_source` if one is configured.

For example, a test can set `data_source: app_db` after `app_db` is added under `data_sources`.

Use the `*.dile.yaml` naming convention and colocate data tests near the application code they protect:

```text
app/orders/orders.py
app/orders/orders.dile.yaml
app/billing/invoices.ts
app/billing/invoices.dile.yaml
```

```yaml
tests:
  - name: no_failed_orders
    description: There should be no failed orders today.
    severity: HIGH
    data_source: app_db
    query: |
      select count(*) as failed_orders
      from orders
      where status = 'failed'
        and created_at >= current_date
    expect: "= 0"

  - name: active_plan_ids
    description: Active subscriptions should only use known plan IDs.
    query: |
      select distinct plan_id
      from subscriptions
      where status = 'active'
      order by plan_id
    expect: "= [1, 2, 3]"
```

`expect` is a comparison string. Supported operators are `=`, `!=`, `>`, `>=`, `<`, and `<=`.

For one-row, one-column query results, Datadile compares the scalar value. For multi-row, one-column results, it compares a list of values. For wider results, it compares dictionaries or lists of dictionaries.
