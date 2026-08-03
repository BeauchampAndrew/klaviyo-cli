"""Auth resolution: profiles from config.toml or KLAVIYO_API_KEY env var."""

import os
import tomllib
from pathlib import Path

from .transport import AuthError, DirectTransport


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", "").strip() or str(Path.home() / ".config")
    return Path(base) / "klaviyo-cli" / "config.toml"


def _load_config() -> dict:
    path = config_path()
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def resolve_transport(profile: str | None) -> DirectTransport:
    cfg = _load_config()
    profiles = cfg.get("profiles", {})

    if profile:
        entry = profiles.get(profile)
        if not entry or not entry.get("api_key"):
            raise AuthError(
                f"Profile '{profile}' not found in {config_path()}.\n"
                f"Known profiles: {', '.join(sorted(profiles)) or '(none)'}"
            )
        return DirectTransport(entry["api_key"])

    env_key = os.environ.get("KLAVIYO_API_KEY", "").strip()
    if env_key:
        return DirectTransport(env_key)

    default = cfg.get("default_profile", "")
    if default and profiles.get(default, {}).get("api_key"):
        return DirectTransport(profiles[default]["api_key"])

    raise AuthError(
        "No Klaviyo credentials found. Either:\n"
        "  export KLAVIYO_API_KEY=pk_...          (single account)\n"
        f"  or create {config_path()} with [profiles.<name>] api_key entries\n"
        "     and pass --profile <name> (or set default_profile)."
    )
