import os
from pathlib import Path
from typing import Any

import yaml

CONFIG_FILENAME = "datadile.yaml"
USER_CONFIG_PATH = Path.home() / ".datadile" / CONFIG_FILENAME
DEFAULT_API_KEY_ENV = "DATADILE_API_KEY"
DEFAULT_DATA_SOURCE_PASSWORD_ENV = "DATABASE_PASSWORD"

CONFIG_TEMPLATE = """\
# Optional. Only required for premium server-backed features.
# api_key_env: DATADILE_API_KEY

default_data_source: main

data_sources:
  main:
    type: postgresql
    host: localhost
    port: 5432
    user: myuser
    database: mydb
    password_env: DATABASE_PASSWORD

# Premium server-backed data source example:
# data_sources:
#   main:
#     id: ds_abc123
"""


def _find_config_file() -> Path | None:
    local = Path(CONFIG_FILENAME)
    if local.exists():
        return local
    if USER_CONFIG_PATH.exists():
        return USER_CONFIG_PATH
    return None


def load_config(required: bool = True) -> dict[str, Any]:
    config_path = _find_config_file()
    if config_path is None:
        if not required:
            return {}
        raise FileNotFoundError(
            f"No config file found. Create one at:\n"
            f"  ./{CONFIG_FILENAME}\n"
            f"  {USER_CONFIG_PATH}\n\n"
            f"Template:\n{CONFIG_TEMPLATE}"
        )

    with config_path.open() as f:
        config = yaml.safe_load(f) or {}

    if not isinstance(config, dict):
        raise ValueError(f"Config file {config_path} must contain a YAML mapping.")
    return config


def get_api_key() -> str | None:
    config = load_config(required=False)
    if config.get("api_key"):
        raise ValueError("Do not put API keys in datadile.yaml. Use api_key_env instead.")

    api_key_env = str(config.get("api_key_env", DEFAULT_API_KEY_ENV))
    return os.environ.get(api_key_env)


def get_api_host() -> str:
    config = load_config(required=False)
    return str(config.get("api_host", "datadile.io"))


def _resolve_data_source_config(data_source: Any, label: str) -> dict[str, Any]:
    if not isinstance(data_source, dict):
        raise KeyError(f"Missing {label} mapping in config file.")

    if data_source.get("id"):
        api_key = get_api_key()
        if not api_key:
            api_key_env = str(load_config(required=False).get("api_key_env", DEFAULT_API_KEY_ENV))
            raise ValueError(
                f"{label}.id requires an API key for server-backed data source access. "
                f"Set the {api_key_env} environment variable."
            )
        return {"id": str(data_source["id"]), "api_key": api_key, "api_host": get_api_host()}

    if data_source.get("password"):
        raise ValueError(f"Do not put data source passwords in datadile.yaml. Use password_env instead.")

    data_source_type = str(data_source.get("type", "postgresql")).lower()
    if data_source_type in {"mongo", "mongodb"}:
        return _resolve_mongodb_data_source_config(data_source, label)

    return _resolve_postgresql_data_source_config(data_source)


def _resolve_postgresql_data_source_config(data_source: dict[str, Any]) -> dict[str, Any]:
    password_env = str(data_source.get("password_env", DEFAULT_DATA_SOURCE_PASSWORD_ENV))
    password = os.environ.get(password_env)
    if not password:
        raise ValueError(f"Missing data source password. Set the {password_env} environment variable.")

    return {
        "type": data_source.get("type", "postgresql"),
        "host": data_source.get("host", "localhost"),
        "port": int(data_source.get("port", 5432)),
        "user": data_source.get("user"),
        "password": password,
        "database": data_source.get("database"),
    }


def _resolve_mongodb_data_source_config(data_source: dict[str, Any], label: str) -> dict[str, Any]:
    if data_source.get("uri"):
        raise ValueError(f"Do not put MongoDB URIs in datadile.yaml. Use uri_env instead.")

    database = data_source.get("database")
    if not database:
        raise ValueError(f"Missing {label}.database in config file.")

    if data_source.get("uri_env"):
        uri_env = str(data_source["uri_env"])
        uri = os.environ.get(uri_env)
        if not uri:
            raise ValueError(f"Missing MongoDB URI. Set the {uri_env} environment variable.")
        return {
            "type": data_source.get("type", "mongodb"),
            "uri": uri,
            "database": database,
        }

    password_env = str(data_source.get("password_env", DEFAULT_DATA_SOURCE_PASSWORD_ENV))
    user = data_source.get("user")
    password = os.environ.get(password_env) if user or data_source.get("password_env") else None
    if user and not password:
        raise ValueError(f"Missing MongoDB password. Set the {password_env} environment variable.")

    config = {
        "type": data_source.get("type", "mongodb"),
        "host": data_source.get("host", "localhost"),
        "port": int(data_source.get("port", 27017)),
        "database": database,
    }
    if user:
        config["user"] = user
        config["password"] = password
    if data_source.get("auth_source"):
        config["auth_source"] = data_source["auth_source"]
    return config


def get_data_source_config(name: str | None = None) -> dict[str, Any]:
    config = load_config()

    data_sources = config.get("data_sources")
    if not isinstance(data_sources, dict):
        raise KeyError("Missing data_sources mapping in config file.")

    data_source_name = name or config.get("default_data_source")
    if not data_source_name:
        raise ValueError("Missing data source. Add default_data_source to datadile.yaml or data_source to the test.")

    data_source_name = str(data_source_name)
    if data_source_name not in data_sources:
        raise KeyError(f"Unknown data source '{data_source_name}' in config file.")

    return _resolve_data_source_config(data_sources[data_source_name], f"data_sources.{data_source_name}")
