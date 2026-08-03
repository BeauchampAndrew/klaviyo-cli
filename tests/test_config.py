import pytest

from klaviyo_cli.config import resolve_transport
from klaviyo_cli.transport import AuthError


def _write_config(tmp_path, monkeypatch, text):
    cfg_dir = tmp_path / "klaviyo-cli"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text(text)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


def test_env_var_wins_when_no_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))  # no config file
    monkeypatch.setenv("KLAVIYO_API_KEY", "pk_env")
    assert resolve_transport(None).api_key == "pk_env"


def test_named_profile(monkeypatch, tmp_path):
    monkeypatch.delenv("KLAVIYO_API_KEY", raising=False)
    _write_config(tmp_path, monkeypatch,
                  '[profiles.acme]\napi_key = "pk_acme"\n')
    assert resolve_transport("acme").api_key == "pk_acme"


def test_default_profile_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("KLAVIYO_API_KEY", raising=False)
    _write_config(tmp_path, monkeypatch,
                  'default_profile = "acme"\n[profiles.acme]\napi_key = "pk_acme"\n')
    assert resolve_transport(None).api_key == "pk_acme"


def test_unknown_profile_errors(monkeypatch, tmp_path):
    _write_config(tmp_path, monkeypatch, '[profiles.acme]\napi_key = "x"\n')
    with pytest.raises(AuthError, match="nope"):
        resolve_transport("nope")


def test_nothing_configured_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("KLAVIYO_API_KEY", raising=False)
    with pytest.raises(AuthError, match="KLAVIYO_API_KEY"):
        resolve_transport(None)
