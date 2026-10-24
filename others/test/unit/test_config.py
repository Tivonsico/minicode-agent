import pytest

from backend.config import Config, ConfigError, load_config


def test_missing_key_and_invalid_budget_fail_before_requests(tmp_path):
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        Config(tmp_path, "")
    assert Config(tmp_path, "secret").context_window == 0
    with pytest.raises(ConfigError):
        Config(tmp_path, "secret", context_window=100, max_output_tokens=100)


def test_env_exact_model_and_secret_repr(tmp_path, monkeypatch):
    for key in ("OPENAI_API_KEY", "MODEL", "CONTEXT_WINDOW", "MAX_OUTPUT_TOKENS"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=test-private-key\nMODEL=GLM-5.3-Flash\nCONTEXT_WINDOW=10000\nMAX_OUTPUT_TOKENS=1000\n"
    )
    config = load_config(tmp_path)
    assert config.model == "GLM-5.3-Flash"
    assert config.context_window == 0  # Legacy environment windows are ignored.
    assert config.max_output_tokens == 0  # Limits come from model metadata, not .env.
    assert config.api_key == "test-private-key"
    assert "test-private-key" not in repr(config)
    monkeypatch.setenv("MODEL", "explicit-override")
    monkeypatch.setenv("MAX_OUTPUT_TOKENS", "not-a-token-limit")
    assert load_config(tmp_path).model == "explicit-override"
    assert load_config(tmp_path).max_output_tokens == 0


def test_url_rejects_credentials_and_full_endpoint(tmp_path):
    for url in ("https://key:secret@host/v4", "https://host/v4/chat/completions"):
        with pytest.raises(ConfigError):
            Config(tmp_path, "secret", base_url=url, context_window=100, max_output_tokens=10)


def test_mysql_requires_explicit_complete_configuration(tmp_path):
    import pytest

    from backend.config import Config, ConfigError

    with pytest.raises(ConfigError, match="MySQL"):
        Config(tmp_path, "test", storage_mode="mysql")
    config = Config(tmp_path, "test", storage_mode="mysql", mysql_host="127.0.0.1",
                    mysql_database="minicode_test", mysql_user="tester", mysql_password="hidden")
    assert config.storage_mode == "mysql" and "hidden" not in repr(config)
