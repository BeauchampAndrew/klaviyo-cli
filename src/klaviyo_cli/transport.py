"""Transport layer: talks to the Klaviyo API. Auth-agnostic commands call this."""

import re
import time

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


def normalize_path(path: str) -> str:
    """Every public Klaviyo endpoint lives under /api/. Accept bare paths like
    'lists?...' or '/lists' so callers don't have to remember the prefix —
    without it the request hits the web app root and returns an HTML page."""
    if not path.startswith("/"):
        path = "/" + path
    if not path.startswith("/api"):
        path = "/api" + path
    return path


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

    # 429 retry: Klaviyo's per-endpoint limits are easy to hit on count
    # endpoints (burst 1/s, steady 15/m on profile_count). Honor Retry-After
    # so loops over many lists/segments survive without per-command backoff.
    MAX_429_RETRIES = 5

    def __init__(self, api_key: str):
        self.api_key = api_key

    def call(self, method: str, path: str, body: dict | None = None,
             revision: str | None = None) -> dict:
        method = method.upper()
        path = normalize_path(path)
        if method == "DELETE":
            ensure_delete_allowed(path)
        headers = {
            "Authorization": f"Klaviyo-API-Key {self.api_key}",
            "revision": revision or DEFAULT_REVISION,
            "Content-Type": "application/json",
        }
        url = f"{KLAVIYO_BASE}{path}"
        if method == "GET":
            send = lambda: requests.get(url, headers=headers, timeout=30)  # noqa: E731
        elif method == "POST":
            send = lambda: requests.post(url, headers=headers, json=body, timeout=30)  # noqa: E731
        elif method == "PATCH":
            send = lambda: requests.patch(url, headers=headers, json=body, timeout=30)  # noqa: E731
        elif method == "PUT":
            send = lambda: requests.put(url, headers=headers, json=body, timeout=30)  # noqa: E731
        elif method == "DELETE":
            send = lambda: requests.delete(url, headers=headers, timeout=30)  # noqa: E731
        else:
            raise AuthError(f"Unsupported HTTP method: {method}")
        return self._with_429_retry(send)

    def upload(self, path: str, files: dict, data: dict | None = None,
               revision: str | None = None) -> dict:
        """Multipart POST (e.g. /api/image-upload/). No JSON Content-Type header:
        requests has to set the multipart boundary itself."""
        headers = {
            "Authorization": f"Klaviyo-API-Key {self.api_key}",
            "revision": revision or DEFAULT_REVISION,
        }
        url = f"{KLAVIYO_BASE}{normalize_path(path)}"
        return self._with_429_retry(
            lambda: requests.post(url, headers=headers, files=files, data=data, timeout=60)
        )

    def _with_429_retry(self, send) -> dict:
        for attempt in range(self.MAX_429_RETRIES + 1):
            resp = send()
            if resp.status_code != 429 or attempt == self.MAX_429_RETRIES:
                return _check_response(resp)
            try:
                wait = int(resp.headers.get("Retry-After", "15"))
            except ValueError:
                wait = 15
            time.sleep(wait + 1)
        raise APIError("Rate limited: exhausted 429 retries")  # unreachable


def upload_via(transport, path: str, files: dict, data: dict | None = None,
               revision: str | None = None) -> dict:
    """Send a multipart upload through a transport, or explain why it can't.

    Host packages may supply transports that only speak JSON (e.g. an API
    gateway proxy); those have no upload() and get a readable error instead
    of an AttributeError.
    """
    upload = getattr(transport, "upload", None)
    if upload is None:
        raise AuthError(
            "This account's connection can't send file uploads (it only relays JSON). "
            "Upload the file in the Klaviyo UI, or use a direct private API key."
        )
    return upload(path, files=files, data=data, revision=revision)
