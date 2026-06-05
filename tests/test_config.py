import pytest
import yaml

from datadile import config


def test_get_data_source_config_supports_mongodb_without_auth(monkeypatch, tmp_path):
    """MongoDB data sources can connect locally without requiring a password."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "datadile.yaml").write_text(
        yaml.safe_dump(
            {
                "default_data_source": "main",
                "data_sources": {
                    "main": {
                        "type": "mongodb",
                        "host": "localhost",
                        "port": 27017,
                        "database": "datadile",
                    }
                },
            }
        )
    )

    assert config.get_data_source_config() == {
        "type": "mongodb",
        "host": "localhost",
        "port": 27017,
        "database": "datadile",
    }


def test_get_data_source_config_supports_mongodb_uri_env(monkeypatch, tmp_path):
    """MongoDB connection URIs are resolved from environment variables only."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")
    (tmp_path / "datadile.yaml").write_text(
        yaml.safe_dump(
            {
                "default_data_source": "main",
                "data_sources": {
                    "main": {
                        "type": "mongodb",
                        "uri_env": "MONGODB_URI",
                        "database": "datadile",
                    }
                },
            }
        )
    )

    assert config.get_data_source_config() == {
        "type": "mongodb",
        "uri": "mongodb://localhost:27017",
        "database": "datadile",
    }


def test_get_data_source_config_rejects_inline_mongodb_uri(monkeypatch, tmp_path):
    """MongoDB URIs are not stored directly because they commonly contain secrets."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "datadile.yaml").write_text(
        yaml.safe_dump(
            {
                "default_data_source": "main",
                "data_sources": {
                    "main": {
                        "type": "mongodb",
                        "uri": "mongodb://user:password@localhost:27017",
                        "database": "datadile",
                    }
                },
            }
        )
    )

    with pytest.raises(ValueError, match="Do not put MongoDB URIs in datadile.yaml"):
        config.get_data_source_config()
