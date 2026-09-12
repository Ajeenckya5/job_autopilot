"""Browser form filler for company-site and Easy Apply application flows.

This module is intentionally conservative: by default it fills what it can and
stops before final submission. Set auto_apply.submit=true to allow a detected
final submit button to be clicked.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

PLATFORM_LOGIN_URLS = {
    "linkedin": "https://www.linkedin.com/login",
    "indeed":   "https://secure.indeed.com/account/login",
    "jobright": "https://jobright.ai/login",
}

# CSS selectors to pre-fill the email field on each platform's login page.
PLATFORM_EMAIL_SELECTORS = {
    "linkedin": "#username",
    "indeed":   "#login-email-input",
    "jobright": "input[type='email'], input[name='email']",
}


def do_platform_login(platform: str, cfg: Any) -> None:
    """Open a persistent browser session for the given platform so the user can
    log in once and save the session. Future auto-apply runs reuse the cookies."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError(
            "playwright is required. Run: "
            "python3 -m pip install -r requirements.txt && "
            "python3 -m playwright install chromium"
        ) from e

    url = PLATFORM_LOGIN_URLS.get(platform)
    if not url:
        print(f"Unknown platform '{platform}'. Choose from: {', '.join(PLATFORM_LOGIN_URLS)}")
        return

    auto = cfg.auto_apply or {}
    user_data_dir = Path(
        auto.get("browser_user_data_dir") or cfg.output_dir / "browser-profile"
    ).expanduser()
    user_data_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nOpening {platform} login page in browser...")
    print(f"Session will be saved to: {user_data_dir}")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(user_data_dir),
            headless=False,
            slow_mo=80,
            accept_downloads=True,
        )
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1000)

        # Pre-fill email to save a step.
        email = cfg.candidate.get("email", "")
        selector = PLATFORM_EMAIL_SELECTORS.get(platform, "")
        if email and selector:
            try:
                page.fill(selector, email, timeout=3000)
            except Exception:
                pass  # field not found or already filled — user handles it

        print(f"\nLog in to {platform} in the browser window that just opened.")
        print("When you are fully logged in, come back here and press ENTER to save.")
        try:
            input()
        except EOFError:
            pass
        context.close()

    print(f"Session saved. Future autopilot runs will apply as your {platform} account.\n")

log = logging.getLogger("autopilot.applicator")

# File inputs whose accept attribute is image-only (profile photo, logo, etc.)
# should NOT receive the resume PDF.
_IMAGE_ACCEPT_RE = re.compile(
    r'\bimage/|\.(?:png|jpe?g|gif|webp|svg|bmp|ico)\b', re.I)

APPLY_RE = re.compile(
    r"\b(easy apply|apply now|apply on employer site|apply for this job|apply)\b",
    re.I,
)
CONTINUE_RE = re.compile(r"\b(next|continue|save and continue|review)\b", re.I)
SUBMIT_RE = re.compile(
    r"\b(submit application|submit|send application|complete application)\b",
    re.I,
)


@dataclass
class BrowserApplyResult:
    status: str
    method: str = "browser"
    destination: str = ""
    screenshot_path: str = ""
    note: str = ""


def apply_with_browser(job: dict, cfg: Any, jd: dict, pitch: dict,
                       resume_pdf: Path) -> BrowserApplyResult:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError(
            "playwright is required for browser auto-apply. Run: "
            "python3 -m pip install -r requirements.txt && "
            "python3 -m playwright install chromium"
        ) from e

    auto = cfg.auto_apply or {}
    submit_enabled = bool(auto.get("submit", False))
    max_steps = int(auto.get("max_steps", 5))
    user_data_dir = Path(auto.get("browser_user_data_dir")
                         or cfg.output_dir / "browser-profile").expanduser()
    screenshot_dir = Path(auto.get("screenshot_dir")
                          or cfg.output_dir / "applications").expanduser()
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    user_data_dir.mkdir(parents=True, exist_ok=True)

    values = _candidate_values(cfg, pitch)
    configured_answers = {str(k).lower(): str(v) for k, v in (auto.get("answers") or {}).items()}
    # Merge learned answers (lower priority) with explicitly configured ones.
    learned = _load_learned_answers(cfg.output_dir)
    answers = {**learned, **configured_answers}
    headless = bool(auto.get("headless", False))
    slow_mo = int(auto.get("slow_mo_ms", 100))

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(user_data_dir),
            headless=headless,
            slow_mo=slow_mo,
            accept_downloads=True,
        )
        page = context.new_page()
        try:
            page.goto(job["url"], wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1200)

            method = "easy_apply" if job.get("source") == "linkedin" else "company_site"
            clicked, page, clicked_label = _click_text(page, context, APPLY_RE)
            if clicked and clicked_label and "easy apply" in clicked_label.lower():
                method = "easy_apply"
            if clicked:
                _settle(page)

            if _looks_login_blocked(page):
                return _finish(context, page, screenshot_dir, job, "blocked", method,
                               "login required before application form")

            status = "ready_for_review"
            session_learned: dict[str, str] = {}
            for _ in range(max_steps):
                _fill_files(page, resume_pdf)
                # Try dropdowns/comboboxes before free-text fields.
                session_learned.update(_fill_comboboxes(page, answers))
                session_learned.update(_fill_fields(page, values, answers))
                session_learned.update(_fill_selects(page, answers))
                session_learned.update(_check_configured_boxes(page, answers))

                if submit_enabled:
                    submitted, page, _ = _click_text(page, context, SUBMIT_RE, timeout=2500)
                    if submitted:
                        _settle(page)
                        status = "submitted"
                        break

                advanced, page, _ = _click_text(page, context, CONTINUE_RE, timeout=1500)
                if not advanced:
                    break
                _settle(page)
                if _looks_login_blocked(page):
                    status = "blocked"
                    break

            # Persist anything the bot successfully filled so future runs reuse it.
            if session_learned:
                _save_learned_answers(cfg.output_dir, session_learned)

            note = (
                "submitted by browser automation"
                if status == "submitted"
                else "filled form where possible; final submit left for review"
            )
            return _finish(context, page, screenshot_dir, job, status, method, note)
        except PlaywrightTimeoutError as e:
            return _finish(context, page, screenshot_dir, job, "error", "browser",
                           f"browser timeout: {e}")
        except Exception as e:
            return _finish(context, page, screenshot_dir, job, "error", "browser",
                           f"browser apply failed: {e}")


def _candidate_values(cfg: Any, pitch: dict) -> dict[str, str]:
    candidate = cfg.candidate
    name = candidate.get("name", "").strip()
    parts = name.split()
    first = parts[0] if parts else ""
    last = parts[-1] if len(parts) > 1 else ""
    cover = "\n\n".join([
        pitch.get("greeting", "Hi there,"),
        pitch.get("body", ""),
        f"Best,\n{name}",
    ]).strip()
    return {
        "first name": first,
        "firstname": first,
        "given name": first,
        "last name": last,
        "lastname": last,
        "family name": last,
        "full name": name,
        "name": name,
        "email": candidate.get("email", ""),
        "e-mail": candidate.get("email", ""),
        "phone": candidate.get("phone", ""),
        "mobile": candidate.get("phone", ""),
        "linkedin": candidate.get("linkedin", ""),
        "linked in": candidate.get("linkedin", ""),
        "cover letter": cover,
        "message": cover,
        "why are you interested": cover,
    }


def _click_text(page: Any, context: Any, pattern: re.Pattern,
                timeout: int = 4000) -> tuple[bool, Any, str]:
    for role in ("button", "link"):
        try:
            locator = page.get_by_role(role, name=pattern).first
            if locator.count() == 0:
                continue
            label = ""
            try:
                label = locator.inner_text(timeout=500).strip()
            except Exception:
                pass
            before = set(context.pages)
            locator.click(timeout=timeout)
            time.sleep(1)
            new_pages = [p for p in context.pages if p not in before]
            if new_pages:
                return True, new_pages[-1], label
            return True, page, label
        except Exception:
            continue
    return False, page, ""


def _fill_files(page: Any, resume_pdf: Path) -> None:
    for inp in page.query_selector_all("input[type='file']"):
        try:
            accept = (inp.get_attribute("accept") or "").lower()
            # Skip image-only file inputs (profile photos, logos, etc.)
            if accept and _IMAGE_ACCEPT_RE.search(accept) and "pdf" not in accept:
                log.debug("skipping image-only file input (accept=%s)", accept)
                continue
            inp.set_input_files(str(resume_pdf))
        except Exception as e:
            log.debug("file upload skipped: %s", e)


def _fill_fields(page: Any, values: dict[str, str], answers: dict[str, str]) -> dict[str, str]:
    filled: dict[str, str] = {}
    selector = (
        "input:not([type='hidden']):not([type='file']):not([type='submit']):"
        "not([type='button']):not([disabled]), textarea:not([disabled])"
    )
    for el in page.query_selector_all(selector):
        try:
            kind = (el.get_attribute("type") or "").lower()
            if kind in {"checkbox", "radio"}:
                continue
            current = el.input_value(timeout=300)
            if current:
                continue
            label = _field_label(el)
            value = _answer_for(label, answers) or _answer_for(label, values)
            if value:
                el.fill(value, timeout=1000)
                key = _label_key(label)
                if key:
                    filled[key] = value
        except Exception as e:
            log.debug("field fill skipped: %s", e)
    return filled


def _fill_selects(page: Any, answers: dict[str, str]) -> dict[str, str]:
    filled: dict[str, str] = {}
    for el in page.query_selector_all("select:not([disabled])"):
        try:
            label = _field_label(el)
            value = _answer_for(label, answers)
            if value:
                try:
                    el.select_option(label=value, timeout=1000)
                except Exception:
                    el.select_option(value=value, timeout=1000)
                key = _label_key(label)
                if key:
                    filled[key] = value
        except Exception as e:
            log.debug("select fill skipped: %s", e)
    return filled


def _check_configured_boxes(page: Any, answers: dict[str, str]) -> dict[str, str]:
    truthy = {"1", "true", "yes", "y", "checked", "check"}
    filled: dict[str, str] = {}
    for el in page.query_selector_all("input[type='checkbox'], input[type='radio']"):
        try:
            label = _field_label(el)
            value = _answer_for(label, answers)
            if value and value.strip().lower() in truthy:
                el.check(timeout=1000)
                key = _label_key(label)
                if key:
                    filled[key] = value
        except Exception as e:
            log.debug("box fill skipped: %s", e)
    return filled


def _field_label(el: Any) -> str:
    """Read the full question/label text for a form element.
    Collects attributes, associated <label>, parent label, and the nearest
    form-group container so screening questions are read completely."""
    try:
        return el.evaluate(
            """node => {
                const attrs = ['name', 'id', 'placeholder', 'aria-label', 'autocomplete', 'type'];
                const parts = attrs.map(a => node.getAttribute(a) || '');
                // Explicit <label for="..."> association.
                if (node.id) {
                  const lbl = document.querySelector(`label[for="${CSS.escape(node.id)}"]`);
                  if (lbl) parts.push(lbl.innerText || '');
                }
                // aria-labelledby
                const lblBy = node.getAttribute('aria-labelledby');
                if (lblBy) {
                  lblBy.split(' ').forEach(id => {
                    const ref = document.getElementById(id);
                    if (ref) parts.push(ref.innerText || '');
                  });
                }
                // Ancestor <label> wrapping the input.
                const parentLabel = node.closest('label');
                if (parentLabel) parts.push(parentLabel.innerText || '');
                // Nearest form group — captures the full question sentence.
                const group = node.closest(
                  '[role="group"], .form-group, .field, .question, ' +
                  '.jobs-easy-apply-form-element, .ia-Questions-item, ' +
                  '.application-question, .field-wrapper, .form-field'
                );
                if (group) parts.push(group.innerText || '');
                return parts.join(' ').toLowerCase();
            }"""
        )
    except Exception:
        return ""


def _label_key(label: str) -> str:
    """Short normalised key for storing a learned answer."""
    first_line = label.split('\n')[0].strip()
    return re.sub(r'\s+', ' ', first_line)[:80]


def _fill_comboboxes(page: Any, answers: dict[str, str]) -> dict[str, str]:
    """Handle ARIA comboboxes and custom dropdown widgets.
    Reads the full question, tries to pick a matching option from the open
    listbox, and falls back to typing only if no option matches."""
    filled: dict[str, str] = {}
    for el in page.query_selector_all(
        '[role="combobox"]:not([disabled]), [role="listbox"]:not([disabled])'
    ):
        try:
            label = _field_label(el)
            value = _answer_for(label, answers)
            if not value:
                continue
            el.click(timeout=1500)
            time.sleep(0.4)
            # Try to find a matching option in the open listbox.
            options = page.query_selector_all('[role="option"], [role="listitem"]')
            matched = None
            for opt in options:
                try:
                    text = opt.inner_text(timeout=300).strip().lower()
                    if value.lower() in text or text in value.lower():
                        matched = opt
                        break
                except Exception:
                    pass
            if matched:
                matched.click(timeout=1000)
                key = _label_key(label)
                if key:
                    filled[key] = value
                log.debug("combobox selected: %s = %s", key, value)
            else:
                # No matching option — type the value and close.
                try:
                    el.fill(value, timeout=1000)
                    key = _label_key(label)
                    if key:
                        filled[key] = value
                except Exception:
                    try:
                        page.keyboard.press("Escape")
                    except Exception:
                        pass
        except Exception as e:
            log.debug("combobox fill skipped: %s", e)
    return filled


def _load_learned_answers(output_dir: Path) -> dict[str, str]:
    """Load answers learned from previous browser sessions."""
    path = output_dir / "learned_answers.yaml"
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return {str(k).lower(): str(v) for k, v in data.items()}
    except Exception as e:
        log.debug("could not load learned answers: %s", e)
        return {}


def _save_learned_answers(output_dir: Path, new_answers: dict[str, str]) -> None:
    """Persist newly discovered field→value pairs so future runs reuse them."""
    if not new_answers:
        return
    path = output_dir / "learned_answers.yaml"
    existing: dict = {}
    if path.exists():
        try:
            with open(path) as f:
                existing = yaml.safe_load(f) or {}
        except Exception:
            pass
    existing.update({k: v for k, v in new_answers.items() if k and v})
    try:
        with open(path, "w") as f:
            yaml.dump(existing, f, default_flow_style=False, allow_unicode=True)
        log.info("learned_answers saved: %d total entries", len(existing))
    except Exception as e:
        log.debug("could not save learned answers: %s", e)


def _answer_for(label: str, answers: dict[str, str]) -> str:
    low = label.lower()
    for key, value in answers.items():
        if key == "name" and re.search(r"\b(company|employer|school|university|reference)\b", low):
            continue
        if key in low:
            return value
    return ""


def _looks_login_blocked(page: Any) -> bool:
    url = page.url.lower()
    if any(token in url for token in ("/login", "/signin", "/sign-in", "/auth")):
        return True
    try:
        title = page.title().lower()
    except Exception:
        title = ""
    return bool(re.search(r"\b(sign in|log in|login required)\b", title))


def _settle(page: Any) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass
    try:
        page.wait_for_timeout(1200)
    except Exception:
        pass


def _finish(context: Any, page: Any, screenshot_dir: Path, job: dict,
            status: str, method: str, note: str) -> BrowserApplyResult:
    shot = ""
    try:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        safe_id = re.sub(r"[^a-zA-Z0-9_.-]+", "-", job.get("id", "job"))[:120]
        path = screenshot_dir / f"{stamp}-{safe_id}-{status}.png"
        page.screenshot(path=str(path), full_page=True)
        shot = str(path)
    except Exception as e:
        log.debug("screenshot failed: %s", e)
    destination = getattr(page, "url", "") or job.get("url", "")
    try:
        context.close()
    except Exception:
        pass
    return BrowserApplyResult(status=status, method=method, destination=destination,
                              screenshot_path=shot, note=note)
