"""IMAP settings for any mailbox domain.

Gmail is one preset. Outlook, Yahoo, iCloud, school, and work addresses
resolve the same way. Unknown domains are probed at sync time.
"""
from __future__ import annotations

from typing import Any

PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "gmail": {
        "label": "Gmail / Google Workspace",
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "mailboxes": ["INBOX", "[Gmail]/All Mail"],
    },
    "outlook": {
        "label": "Outlook / Microsoft 365 / Hotmail",
        "imap_host": "outlook.office365.com",
        "imap_port": 993,
        "mailboxes": ["INBOX"],
    },
    "yahoo": {
        "label": "Yahoo Mail",
        "imap_host": "imap.mail.yahoo.com",
        "imap_port": 993,
        "mailboxes": ["INBOX"],
    },
    "icloud": {
        "label": "iCloud",
        "imap_host": "imap.mail.me.com",
        "imap_port": 993,
        "mailboxes": ["INBOX"],
    },
    "aol": {
        "label": "AOL",
        "imap_host": "imap.aol.com",
        "imap_port": 993,
        "mailboxes": ["INBOX"],
    },
    "zoho": {
        "label": "Zoho Mail",
        "imap_host": "imap.zoho.com",
        "imap_port": 993,
        "mailboxes": ["INBOX"],
    },
    "fastmail": {
        "label": "Fastmail",
        "imap_host": "imap.fastmail.com",
        "imap_port": 993,
        "mailboxes": ["INBOX"],
    },
    "gmx": {
        "label": "GMX",
        "imap_host": "imap.gmx.com",
        "imap_port": 993,
        "mailboxes": ["INBOX"],
    },
    "proton": {
        "label": "Proton Mail",
        "imap_host": "imap.proton.me",
        "imap_port": 993,
        "mailboxes": ["INBOX"],
    },
}

DOMAIN_PROVIDERS: dict[str, str] = {
    "gmail.com": "gmail",
    "googlemail.com": "gmail",
    "outlook.com": "outlook",
    "hotmail.com": "outlook",
    "live.com": "outlook",
    "msn.com": "outlook",
    "office365.com": "outlook",
    "yahoo.com": "yahoo",
    "ymail.com": "yahoo",
    "rocketmail.com": "yahoo",
    "icloud.com": "icloud",
    "me.com": "icloud",
    "mac.com": "icloud",
    "aol.com": "aol",
    "zoho.com": "zoho",
    "zohomail.com": "zoho",
    "fastmail.com": "fastmail",
    "gmx.com": "gmx",
    "gmx.net": "gmx",
    "proton.me": "proton",
    "protonmail.com": "proton",
}


def email_domain(address: str) -> str:
    raw = (address or "").strip().lower()
    if "@" not in raw:
        return ""
    return raw.rsplit("@", 1)[-1].strip()


def detect_provider(address: str) -> str:
    return DOMAIN_PROVIDERS.get(email_domain(address), "auto")


def mailboxes_for_host(host: str) -> list[str]:
    host = (host or "").strip().lower()
    if host in {"imap.gmail.com", "imap.googlemail.com"}:
        return ["INBOX", "[Gmail]/All Mail"]
    return ["INBOX"]


def resolve_mail_profile(
    address: str,
    *,
    provider: str = "auto",
    imap_host: str = "",
    imap_port: int | str = 993,
) -> dict[str, Any]:
    """Pick IMAP host and folders from an email address and optional overrides."""
    host = str(imap_host or "").strip()
    prov = str(provider or "auto").strip().lower()
    if prov in {"other", "imap", "custom"}:
        prov = "auto"
    try:
        port = int(imap_port or 993)
    except (TypeError, ValueError):
        port = 993
    if port <= 0:
        port = 993

    if host:
        kind = prov if prov in PROVIDER_PRESETS else "imap"
        return {
            "provider": kind,
            "imap_host": host,
            "imap_port": port,
            "mailboxes": mailboxes_for_host(host),
        }

    if prov in {"auto", "detect", ""}:
        prov = detect_provider(address)

    if prov in PROVIDER_PRESETS:
        spec = PROVIDER_PRESETS[prov]
        return {
            "provider": prov,
            "imap_host": spec["imap_host"],
            "imap_port": int(spec["imap_port"]),
            "mailboxes": list(spec["mailboxes"]),
        }

    return {
        "provider": "auto",
        "imap_host": "",
        "imap_port": 993,
        "mailboxes": ["INBOX"],
    }


def candidate_endpoints(address: str, settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Ordered IMAP endpoints to try for this mailbox.

    An explicit host is used alone. Known consumer domains use one preset.
    Any other domain (school, work, custom) is probed across Microsoft 365,
    Google Workspace, then imap/mail on that domain.
    """
    settings = dict(settings or {})
    username = (
        str(settings.get("username") or address or "").strip()
    )
    profile = resolve_mail_profile(
        username or address,
        provider=str(settings.get("provider") or "auto"),
        imap_host=str(settings.get("imap_host") or ""),
        imap_port=settings.get("imap_port") or 993,
    )
    configured_boxes = settings.get("mailboxes")
    if isinstance(configured_boxes, list) and configured_boxes:
        boxes = [str(b) for b in configured_boxes if str(b).strip()]
    else:
        boxes = list(profile["mailboxes"])

    if profile["imap_host"]:
        return [{
            "provider": profile["provider"],
            "imap_host": profile["imap_host"],
            "imap_port": profile["imap_port"],
            "mailboxes": boxes or ["INBOX"],
        }]

    domain = email_domain(username or address)
    guesses = [
        ("outlook", "outlook.office365.com", 993, ["INBOX"]),
        ("gmail", "imap.gmail.com", 993, ["INBOX", "[Gmail]/All Mail"]),
    ]
    if domain:
        guesses.append(("imap", f"imap.{domain}", 993, ["INBOX"]))
        guesses.append(("imap", f"mail.{domain}", 993, ["INBOX"]))
    return [
        {
            "provider": kind,
            "imap_host": host,
            "imap_port": port,
            "mailboxes": folders,
        }
        for kind, host, port, folders in guesses
    ]
