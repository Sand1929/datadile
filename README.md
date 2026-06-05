# datadile

Datadile helps teams make database assumptions explicit, testable, and visible to both humans and coding agents.

Define lightweight data tests in YAML, run them locally with the CLI, and catch unsafe database states before they break application code, migrations, backfills, or downstream workflows.

[Datadile Cloud](https://datadile.io/) turns those tests into a shared data-assumption layer for your team and agents. It records test runs, shows which assumptions are passing or failing, schedules automated checks, sends alerts, and surfaces anomalies so agents can use real data context when changing code.

## Quick Start

Install Datadile:

```bash
pip install datadile
```

Add your project config file. Run this from your project or repo root, or use `--global` to add a user-level config file:

```bash
datadile init [--global]
```

Then fill in `datadile.yaml` with your database connection details:

```yaml
default_data_source: main

data_sources:
  main:
    type: postgresql
    host: localhost
    port: 5432
    user: myuser
    database: mydb
    password_env: DATABASE_PASSWORD
```

Put the password in an environment variable, not in `datadile.yaml`:

```bash
export DATABASE_PASSWORD='your_password_here'
```

Recommended: install the Datadile coding-agent skill so your agent can write data tests using Datadile's conventions:

```bash
datadile install-skill [--global]
```

The default install target is `.agents/skills/`. For agents that don't support `.agents/skills/` (e.g., Claude Code), pass `--agent`, for example:

```bash
datadile install-skill --agent claude
```

Then ask your coding agent to create data tests for your project, for example:

```text
Add tests for the key data assumptions in this code.
```

If you want to try Datadile without a coding agent, create your first data test in `smoke.dile.yaml`:

```yaml
# smoke.dile.yaml
tests:
  - name: database_connection_works
    description: Datadile can connect to the configured database and run a read-only query.
    query: |
      select 1 as value
    expect: "= 1"
```

Run the tests:

```bash
datadile test
```

Datadile prints a results table and a summary such as `1 passed, 0 failed`.
To save full, untruncated results to JSON, pass `--results-file`:

```bash
datadile test --results-file datadile-results.json
```

## Data Tests

Tests are defined in YAML files named `*.dile.yaml`. Each test has `name`, `description`, `query`, and `expect`. `severity` is optional and defaults to `MEDIUM`; valid values are `LOW`, `MEDIUM`, and `HIGH`. `data_source` is optional and references a named source from `data_sources`; otherwise Datadile uses `default_data_source` if one is configured. `identity` is optional and gives [Datadile Cloud](https://datadile.io/) a stable identifier for the test, even if the file path changes.

Use the `*.dile.yaml` naming convention and colocate data tests near the application code they protect:

```text
app/orders/orders.py
app/orders/orders.dile.yaml
app/billing/invoices.ts
app/billing/invoices.dile.yaml
```

For example, a test can set `data_source: app_db` after `app_db` is added under `data_sources`:

```yaml
tests:
  - name: no_failed_orders
    identity: orders.no_failed_orders
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

Use `row_count` when the query should return inspectable rows but the assertion is about how many rows were returned:

```yaml
tests:
  - name: failed_orders_are_limited
    description: There should be at most one failed order today, with rows shown on failure.
    query: |
      select id, status, created_at
      from orders
      where status = 'failed'
        and created_at >= current_date
      order by created_at desc
    expect: "row_count <= 1"
```

## Configuration

Datadile looks for a YAML config file in two locations. Local config takes precedence:

1. `./datadile.yaml` (current directory)
2. `~/.datadile/datadile.yaml` (user-level)

Create a starter config with `datadile init`, then fill in your values. To write it somewhere else, pass a destination path. Existing files are not overwritten unless you pass `--force`.

```bash
datadile init path/to/datadile.yaml
datadile init --force
```

For a user-level config instead of a project-local one:

```bash
datadile init --global
```

```yaml
default_data_source: main

data_sources:
  main:
    type: postgresql
    host: localhost
    port: 5432
    user: myuser
    database: mydb
    password_env: DATABASE_PASSWORD
```

Put data source passwords in environment variables, not in `datadile.yaml`:

```bash
export DATABASE_PASSWORD='your_password_here'
```

## Usage

```bash
datadile test
datadile test <path/to/file.dile.yaml>
datadile test --results-file datadile-results.json
```

With no path, Datadile recursively discovers only files matching `*.dile.yaml` from the current directory. Other YAML files, such as `docker-compose.yaml`, GitHub Actions workflows, Helm values, and OpenAPI specs, are ignored.

Use `datadile context` to show existing data tests for the same data source and overlapping tables or columns:

```bash
datadile context --data-source app_db --table orders
datadile context --data-source app_db --column orders.status
datadile context --data-source app_db --table orders --format json
```

The context command loads local `*.dile.yaml` files. If an API key is configured, it also fetches matching uploaded tests from [Datadile Cloud](https://datadile.io/) to determine what tests are currently passing and failing.

## Coding Agent Skill

Datadile includes a bundled coding-agent skill with Datadile-specific guidance. For many projects, the fastest path is to add `datadile.yaml`, install the skill, and ask your coding agent to write the first `*.dile.yaml` tests. The skill is optional and not required to run data tests.

Install it into the current working directory with:

```bash
datadile install-skill
```

By default, this writes the skill to `.agents/skills/datadile/SKILL.md` under the directory where you run the command. The command shows the full destination path and asks for confirmation before writing the file.

To install for a different coding agent, pass `--agent`, for example:

```bash
datadile install-skill --agent claude
```

To install into the selected agent's user-level skills directory instead, pass `--global`:

```bash
datadile install-skill --global
datadile install-skill --agent claude --global
```

To install it somewhere else, pass a destination path:

```bash
datadile install-skill path/to/SKILL.md
```

## [Datadile Cloud](https://datadile.io/)

If an API key is configured, Datadile records completed test runs in [Datadile Cloud](https://datadile.io/). Datadile monitors runs to alert you about failures and anomalies within your tests. Datadile also draws on this data to provide context to your coding agent, allowing it to take into account what data assumptions are failing while writing your code.

You can schedule tests to run automatically by connecting your data sources to Datadile Cloud, either through encrypted pipelines or within your own private cloud.

To enable cloud features, put the API key in an environment variable and reference that variable from `datadile.yaml`:

```bash
export DATADILE_API_KEY='your_api_key_here'
```

```yaml
api_key_env: DATADILE_API_KEY
```

Data source entries can also reference an ID if the connection details are stored on your Datadile account:

```yaml
api_key_env: DATADILE_API_KEY

default_data_source: warehouse

data_sources:
  warehouse:
    id: ds_abc123
```
