"""Transport layer: talks to the Klaviyo API. Auth-agnostic commands call this."""

import re

import requests

KLAVIYO_BASE = "https://a.klaviyo.com"
DEFAULT_REVISION = "2025-07-15"


class APIError(Exception):
    """Raised when a Klaviyo API call fails."""


class AuthError(Exception):
    """Raised when credentials are missing or an operation is blocked."""


# DELETE is gated to an allowlist of paths we have intentionally vetted as safe.
# DO NOT add catalog-deletion endpoints lightly: deleting a profile, list,
# segment, or campaign in Klaviyo is destructive and often irreversible from
# the API. To add a new entry, treat it like a security review — confirm the
# operation cannot cascade into losing subscribers, sent-campaign history, or
# flow definitions, and that the user is asking to delete the specific object,
# not its contents.
DELETE_ALLOWED_PATHS = [
    re.compile(r"^/api/templates/[A-Za-z0-9_-]+/?$"),
]


def ensure_delete_allowed(path: str) -> None:
    if not any(p.match(path) for p in DELETE_ALLOWED_PATHS):
        raise AuthError(
            f"DELETE on {path} is not allowed. Klaviyo's destructive-API "
            "operations (profiles, lists, segments, campaigns, flows) are "
            "blocked at the CLI level to prevent accidental data loss. "
            "If this delete is intentional and safe, add a pattern to "
            "DELETE_ALLOWED_PATHS in klaviyo_cli/transport.py."
        )


def _extract_error(resp: requests.Response) -> str:
    try:
        data = resp.json()
        # Klaviyo
        if "errors" in data:
            errors = data["errors"]
            if errors:
                return errors[0].get("detail", str(errors[0]))
        # Generic
        if "message" in data:
            return data["message"]
    except (ValueError, KeyError):
        pass
    return f"HTTP {resp.status_code}: {resp.text[:200]}"


def _check_response(resp: requests.Response) -> dict:
    if resp.status_code >= 400:
        raise APIError(_extract_error(resp))
    try:
        return resp.json()
    except ValueError:
        return {"_raw": resp.text}


class DirectTransport:
    """Calls a.klaviyo.com directly with a private API key."""

    def __init__(self, api_key: str):
        self.api_key = api_key

    def call(self, method: str, path: str, body: dict | None = None,
             revision: str | None = None) -> dict:
        method = method.upper()
        if method == "DELETE":
            ensure_delete_allowed(path)
        headers = {
            "Authorization": f"Klaviyo-API-Key {self.api_key}",
            "revision": revision or DEFAULT_REVISION,
            "Content-Type": "application/json",
        }
        url = f"{KLAVIYO_BASE}{path}"
        if method == "GET":
            resp = requests.get(url, headers=headers, timeout=30)
        elif method == "POST":
            resp = requests.post(url, headers=headers, json=body, timeout=30)
        elif method == "PATCH":
            resp = requests.patch(url, headers=headers, json=body, timeout=30)
        elif method == "PUT":
            resp = requests.put(url, headers=headers, json=body, timeout=30)
        elif method == "DELETE":
            resp = requests.delete(url, headers=headers, timeout=30)
        else:
            raise AuthError(f"Unsupported HTTP method: {method}")
        return _check_response(resp)
