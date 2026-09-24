"""Mac-mode key storage and provider proxy.

Keys go in the macOS Keychain. The browser sends the job payload, and this
process attaches the key. Payloads are not logged.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

SERVICE = "job-autopilot-llm"
ALLOWED_HOSTS = {
    "generativelanguage.googleapis.com",
    "api.groq.com",
    "api.openai.com",
    "api.anthropic.com",
    "openrouter.ai",
}
OLLAMA_PORTS = {11434}
KNOWN = {"gemini", "groq", "openai", "anthropic", "openrouter", "ollama"}
SECRET = re.compile(r"(sk-|AIza|sk-or-|gsk_|Bearer )[A-Za-z0-9_\-]{6,}")


def scrub(text: str) -> str:
    return SECRET.sub("[key]", str(text or ""))[:180]


def _account(provider: str) -> str:
    name = str(provider or "").strip().lower()
    if name not in KNOWN:
        raise ValueError("Unknown provider.")
    return name


def keychain_set(provider: str, secret: str, runner=subprocess.run) -> None:
    account = _account(provider)
    if not str(secret or "").strip():
        raise ValueError("Enter a key.")
    result = runner(
        ["security", "add-generic-password", "-U", "-s", SERVICE, "-a", account, "-w", secret],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError("Keychain could not store the key.")


def keychain_get(provider: str, runner=subprocess.run) -> str:
    account = _account(provider)
    result = runner(
        ["security", "find-generic-password", "-s", SERVICE, "-a", account, "-w"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def keychain_status(runner=subprocess.run) -> dict:
    return {provider: bool(keychain_get(provider, runner)) for provider in sorted(KNOWN)}


def _yaml(path: Path) -> dict:
    try:
        import yaml
    except Exception:
        return {}
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def migrate_config(path: Path, runner=subprocess.run) -> list[str]:
    data = _yaml(path)
    moved = []
    pairs = []
    provider = str(data.get("llm_provider") or "").strip().lower()
    key = str(data.get("llm_api_key") or "").strip()
    if provider in KNOWN and key:
        pairs.append((provider, key))
    for row in data.get("llm_providers") or []:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("type") or "").strip().lower()
        value = str(row.get("api_key") or "").strip()
        if kind in KNOWN and value:
            pairs.append((kind, value))
    xai = data.get("xai") if isinstance(data.get("xai"), dict) else {}
    if provider == "xai" and not pairs and str(xai.get("api_key") or "").strip():
        return moved
    for kind, value in pairs:
        if keychain_get(kind, runner):
            continue
        keychain_set(kind, value, runner)
        moved.append(kind)
    return moved


def _allowed(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host in {"127.0.0.1", "localhost"}:
        if parsed.port not in OLLAMA_PORTS or not parsed.path.startswith("/api/"):
            raise ValueError("That local address is not allowed.")
        return
    if parsed.scheme != "https" or host not in ALLOWED_HOSTS:
        raise ValueError("That provider host is not allowed.")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        raise ValueError("Redirect refused.")


def forward(spec: dict, runner=subprocess.run, opener=None) -> dict:
    provider = _account(spec.get("provider") or "")
    url = str(spec.get("url") or "")
    _allowed(url)
    headers = {}
    for key, value in (spec.get("headers") or {}).items():
        if re.search(r"authorization|api-key", str(key), re.I):
            continue
        headers[str(key)] = str(value)
    secret = keychain_get(provider, runner) if provider != "ollama" else ""
    if provider != "ollama" and not secret:
        raise ValueError("Save a key in Keychain first.")
    if provider == "gemini" and secret:
        headers["x-goog-api-key"] = secret
    elif provider == "anthropic" and secret:
        headers["x-api-key"] = secret
    elif secret:
        headers["authorization"] = f"Bearer {secret}"
    method = str(spec.get("method") or ("POST" if spec.get("body") is not None else "GET")).upper()
    body = spec.get("body")
    data = json.dumps(body).encode("utf-8") if body is not None and method != "GET" else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    agent = opener or urllib.request.build_opener(_NoRedirect)
    try:
        with agent.open(request, timeout=25) as response:
            raw = response.read(1_000_000)
            return {"status": response.status, "body": json.loads(raw.decode("utf-8") or "{}")}
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", "replace")
        try:
            body = json.loads(raw or "{}")
        except json.JSONDecodeError:
            body = {"error": scrub(raw)}
        return {"status": error.code, "body": body}
    except ValueError:
        raise
    except Exception as error:
        raise ValueError(scrub(str(error)) or "Provider request failed.") from None
