#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PhoneReg - register an account straight from the terminal.

Flow (as specified):
  1. welcome wheel popup  -> answer y (play) or n (skip)
  2. enter a 10-digit mobile number   (field: phoneNumberOrEmail)
  3. enter the OTP code received by SMS (field: sms-code)
  4. spin the wheel again (reward spin)

Works with plain URLs only (no API access): it loads the site's HTML pages,
fills the forms like a browser would (hidden/CSRF fields included) and keeps
the session cookies so the OTP stays tied to your request.

Authorized use only: run against sites you own or have permission to use.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    sys.stderr.write("Missing packages. Run:  pip install -r requirements.txt\n")
    raise SystemExit(2)


APP_NAME = "PhoneReg"
VERSION = "1.3.0"
DEFAULT_CONFIG = "site_config.json"
EXAMPLE_CONFIG_NAME = "site_config.example.json"
DEFAULT_LOG = "registration_log.jsonl"

RESEND = "__RESEND__"

# Wheel popup selectors, taken from the provided markup:
#   div.css-3z4mse  -> GO control       (answer "y")
#   div.css-bwzg6b  -> dismiss control  (answer "n")
#   div.css-c6ko63  -> "Login To Win Big Rewards"
DEFAULT_WHEEL = {
    "go_selector": ".css-3z4mse",
    "skip_selector": ".css-bwzg6b",
    "title_selector": ".css-c6ko63",
    "title_contains": "Login To Win Big Rewards",
}

DEFAULT_ERROR_KEYWORDS = [
    "invalid code", "invalid otp", "incorrect code", "incorrect otp",
    "code is invalid", "code has expired", "otp has expired",
    "wrong code", "wrong otp", "verification failed", "does not match",
    "not match", "invalid pin", "too many attempts", "expired code",
]

# Field auto-detection hints (matched against name/id/placeholder/label)
MOBILE_HINTS = {
    "mobilenumber": 6, "phonenumber": 6, "msisdn": 6, "cellphone": 5,
    "mobile": 5, "phone": 4, "telephone": 4, "contactnumber": 4,
    "tel": 2, "contact": 2,
}
MOBILE_HARD_EXCLUDE = (
    "country", "postal", "zip", "firstname", "lastname", "username",
    "password", "city", "address", "fax", "otp", "code",
)
# soft: penalize but do not reject (e.g. "phoneNumberOrEmail" names both)
MOBILE_SOFT_EXCLUDE = ("email",)

OTP_HINTS = {
    "otp": 7, "smscode": 7, "otpcode": 7, "onetimepassword": 6,
    "verificationcode": 6, "verifycode": 6, "confirmationcode": 5,
    "onetimecode": 5, "passcode": 5, "securitycode": 4, "authcode": 4,
    "pin": 3, "code": 2,
}
OTP_HARD_EXCLUDE = ("country", "postal", "area", "region", "csrf", "authenticity", "nonce")
OTP_SOFT_EXCLUDE = ()

RESEND_HINTS = ("resend", "send again", "sendagain", "new code", "newcode", "request code")


# ----------------------------------------------------------------------------
# Terminal output
# ----------------------------------------------------------------------------

USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def enable_windows_ansi() -> None:
    """Turn on VT100 escape codes so colors work in Windows 10+ cmd."""
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def bold(t: str) -> str:
    return paint(t, "1")


def dim(t: str) -> str:
    return paint(t, "2")


def red(t: str) -> str:
    return paint(t, "31")


def green(t: str) -> str:
    return paint(t, "32")


def yellow(t: str) -> str:
    return paint(t, "33")


def cyan(t: str) -> str:
    return paint(t, "36")


def frame_line(text: str, width: int = 58, code: str = "0") -> None:
    print(cyan("|") + paint(text.ljust(width), code) + cyan("|"))


def banner() -> None:
    line = "+" + "-" * 58 + "+"
    print(cyan(line))
    frame_line(f"  {APP_NAME}  v{VERSION}", code="1;36")
    frame_line("  wheel -> 10-digit mobile -> OTP -> wheel again")
    frame_line("  works with plain URLs - no API access required")
    print(cyan(line))


def step(index: int, total: int, message: str) -> None:
    print(f"{cyan(f'[{index:02d}/{total:02d}]')} {bold(message)}")


def ok(message: str) -> None:
    print(f"  {green('OK   ')} {message}")


def info(message: str) -> None:
    print(f"  {dim('.... ')} {message}")


def warn(message: str) -> None:
    print(f"  {yellow('WARN ')} {message}")


def err(message: str) -> None:
    print(f"  {red('FAIL ')} {message}")


def kv(key: str, value: str) -> None:
    print(f"       {dim(key.ljust(14))} {value}")


# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------


@dataclass
class SuccessRule:
    status: List[int] = field(default_factory=lambda: [200, 201, 202, 302])
    body_contains: List[str] = field(default_factory=list)
    body_not_contains: List[str] = field(default_factory=list)
    redirect_contains: List[str] = field(default_factory=list)


@dataclass
class WheelConfig:
    enabled: bool = True
    go_selector: str = DEFAULT_WHEEL["go_selector"]
    skip_selector: str = DEFAULT_WHEEL["skip_selector"]
    title_selector: str = DEFAULT_WHEEL["title_selector"]
    title_contains: str = DEFAULT_WHEEL["title_contains"]
    replay_url: str = ""          # page showing the wheel again after registration
    wait_ms: int = 4500
    screenshot: str = "wheel_welcome.png"
    screenshot2: str = "wheel_reward.png"


@dataclass
class SiteConfig:
    site: str
    register_url: str
    mobile_field: str = ""
    mobile_length: int = 10
    otp_field: str = ""
    resend_selector: str = ""     # e.g. ".css-hdbbym" (Resend verification code)
    send_marker: str = ""         # e.g. "Verification code sent successfully"
    otp_url: str = ""
    otp_length: int = 0
    static_fields: Dict[str, str] = field(default_factory=dict)
    success: SuccessRule = field(default_factory=SuccessRule)
    wheel: WheelConfig = field(default_factory=WheelConfig)
    headers: Dict[str, str] = field(default_factory=dict)
    timeout: int = 20

    @classmethod
    def load(cls, path: str) -> "SiteConfig":
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"Config not found: {path}\n"
                f"Run 'python register.py --init' to create {EXAMPLE_CONFIG_NAME}."
            )
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)

        for required in ("site", "register_url"):
            if required not in raw:
                raise ValueError(f"Config is missing required key: {required!r}")

        s_raw = raw.get("success", {}) or {}
        status = s_raw.get("status", [200, 201, 202, 302])
        if isinstance(status, int):
            status = [status]
        success = SuccessRule(
            status=list(status),
            body_contains=_as_list(s_raw.get("body_contains")),
            body_not_contains=_as_list(s_raw.get("body_not_contains")),
            redirect_contains=_as_list(s_raw.get("redirect_contains")),
        )

        w_raw = raw.get("wheel", {}) or {}
        wheel = WheelConfig(
            enabled=bool(w_raw.get("enabled", True)),
            go_selector=w_raw.get("go_selector") or DEFAULT_WHEEL["go_selector"],
            skip_selector=w_raw.get("skip_selector") or DEFAULT_WHEEL["skip_selector"],
            title_selector=w_raw.get("title_selector") or DEFAULT_WHEEL["title_selector"],
            title_contains=w_raw.get("title_contains") or DEFAULT_WHEEL["title_contains"],
            replay_url=w_raw.get("replay_url", "") or "",
            wait_ms=int(w_raw.get("wait_ms", 4500)),
            screenshot=w_raw.get("screenshot") or "wheel_welcome.png",
            screenshot2=w_raw.get("screenshot2") or "wheel_reward.png",
        )

        return cls(
            site=raw["site"],
            register_url=raw["register_url"],
            mobile_field=raw.get("mobile_field", "") or "",
            mobile_length=int(raw.get("mobile_length", 10) or 10),
            otp_field=raw.get("otp_field", "") or "",
            resend_selector=raw.get("resend_selector", "") or "",
            send_marker=raw.get("send_marker", "") or "",
            otp_url=raw.get("otp_url", "") or "",
            otp_length=int(raw.get("otp_length", 0) or 0),
            static_fields=dict(raw.get("static_fields", {}) or {}),
            success=success,
            wheel=wheel,
            headers=dict(raw.get("headers", {}) or {}),
            timeout=int(raw.get("timeout", 20)),
        )


def _as_list(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


# ----------------------------------------------------------------------------
# HTML form handling
# ----------------------------------------------------------------------------


@dataclass
class FormTarget:
    page_url: str
    action: str
    method: str
    payload: Dict[str, str]
    field: str


@dataclass
class PageForms:
    mobile: Optional[FormTarget] = None
    otp: Optional[FormTarget] = None
    resend_url: Optional[str] = None
    title: str = ""
    candidates: List[Tuple[str, int, int]] = field(default_factory=list)


def _form_defaults(form) -> Dict[str, str]:
    """Copy hidden inputs (CSRF tokens etc.) and default values."""
    payload: Dict[str, str] = {}
    for node in form.find_all("input"):
        name = node.get("name")
        if not name:
            continue
        itype = (node.get("type") or "text").lower()
        if itype in ("submit", "button", "image", "file", "reset"):
            continue
        if itype in ("checkbox", "radio"):
            if node.has_attr("checked"):
                payload[name] = node.get("value", "on")
            continue
        payload[name] = node.get("value", "")

    for node in form.find_all("select"):
        name = node.get("name")
        if not name:
            continue
        chosen = node.find("option", selected=True) or node.find("option")
        payload[name] = chosen.get("value", "") if chosen else ""

    for node in form.find_all("textarea"):
        name = node.get("name")
        if name:
            payload[name] = node.get_text() or ""
    return payload


def _submit_control(form) -> Optional[Tuple[str, str]]:
    for node in form.find_all(["input", "button"]):
        itype = (node.get("type") or "").lower()
        if node.name == "input" and itype != "submit":
            continue
        if node.name == "button" and itype not in ("submit", ""):
            continue
        name = node.get("name")
        if name:
            value = node.get("value") or node.get_text(strip=True) or "submit"
            return name, value
    return None


def _visible_fields(form):
    for node in form.find_all(["input", "select", "textarea"]):
        name = node.get("name")
        if not name:
            continue
        itype = (node.get("type") or "text").lower()
        if itype in ("hidden", "submit", "button", "image", "file", "reset"):
            continue
        yield name, node


def _haystack(node, soup) -> str:
    parts = [
        node.get("name", ""), node.get("id", ""), node.get("placeholder", ""),
        node.get("aria-label", ""), node.get("autocomplete", ""),
        node.get("title", ""), " ".join(node.get("class", []) or []),
    ]
    node_id = node.get("id")
    if node_id:
        label = soup.find("label", attrs={"for": node_id})
        if label:
            parts.append(label.get_text(" ", strip=True))
    return re.sub(r"[^a-z0-9]+", "", " ".join(parts).lower())


def _score_field(
    name: str,
    hay: str,
    exact: str,
    hints: Dict[str, int],
    hard_exclude: Tuple[str, ...],
    soft_exclude: Tuple[str, ...],
) -> int:
    if exact and name == exact:
        return 1000
    if any(x in hay for x in hard_exclude):
        return 0
    scores = [w for h, w in hints.items() if h in hay]
    best = max(scores) if scores else 0
    if best and any(x in hay for x in soft_exclude):
        best -= 1
    return best


def _find_resend(soup, page_url: str) -> Optional[str]:
    for tag in soup.find_all(["a", "button"]):
        blob = ((tag.get_text(" ", strip=True) or "") + " " + (tag.get("href") or "")).lower()
        if any(k in blob for k in RESEND_HINTS):
            href = tag.get("href") or ""
            if tag.name == "a" and href and not href.startswith("#"):
                return urljoin(page_url, href)
    return None


def parse_page(html: str, page_url: str, cfg: SiteConfig) -> PageForms:
    soup = BeautifulSoup(html, "html.parser")
    page = PageForms()
    title = soup.find("title")
    page.title = title.get_text(strip=True) if title else ""

    best_mobile = 0
    best_otp = 0

    for form in soup.find_all("form"):
        action = urljoin(page_url, form.get("action") or "")
        method = (form.get("method") or "POST").upper()
        payload = _form_defaults(form)
        control = _submit_control(form)
        if control:
            payload[control[0]] = control[1]

        for name, node in _visible_fields(form):
            hay = _haystack(node, soup)
            m_score = _score_field(name, hay, cfg.mobile_field,
                                  MOBILE_HINTS, MOBILE_HARD_EXCLUDE, MOBILE_SOFT_EXCLUDE)
            o_score = _score_field(name, hay, cfg.otp_field,
                                  OTP_HINTS, OTP_HARD_EXCLUDE, OTP_SOFT_EXCLUDE)
            page.candidates.append((name, m_score, o_score))
            if m_score > best_mobile:
                best_mobile = m_score
                page.mobile = FormTarget(page_url, action, method, dict(payload), name)
            if o_score > best_otp:
                best_otp = o_score
                page.otp = FormTarget(page_url, action, method, dict(payload), name)

    page.resend_url = _find_resend(soup, page_url)
    return page


def carry_mobile(target: FormTarget, mobile_field: str, mobile: str) -> None:
    if mobile_field and not target.payload.get(mobile_field, ""):
        target.payload[mobile_field] = mobile


def _submit(session, target: FormTarget, timeout: int, *overrides: Dict[str, str]):
    data = dict(target.payload)
    for over in overrides:
        data.update(over)
    headers = {"Referer": target.page_url}
    if target.method == "GET":
        return session.get(target.action, params=data, headers=headers, timeout=timeout)
    return session.post(target.action, data=data, headers=headers, timeout=timeout)


# ----------------------------------------------------------------------------
# Welcome wheel popup (first spin and reward spin)
# ----------------------------------------------------------------------------


@dataclass
class WheelPresence:
    found: bool
    title: str = ""
    go_selector: str = ""
    skip_selector: str = ""


def detect_wheel(html: str, cfg: WheelConfig) -> WheelPresence:
    """Look for the popup using the selectors from the provided markup."""
    soup = BeautifulSoup(html or "", "html.parser")

    go = soup.select_one(cfg.go_selector) if cfg.go_selector else None
    skip = soup.select_one(cfg.skip_selector) if cfg.skip_selector else None

    title_text = ""
    title_ok = True
    if cfg.title_selector:
        node = soup.select_one(cfg.title_selector)
        if node is not None:
            title_text = node.get_text(" ", strip=True)
        if cfg.title_contains:
            title_ok = bool(title_text) and (
                cfg.title_contains.lower() in title_text.lower()
            )

    found = go is not None and skip is not None and title_ok
    return WheelPresence(
        found=found,
        title=title_text,
        go_selector=cfg.go_selector if go is not None else "",
        skip_selector=cfg.skip_selector if skip is not None else "",
    )


def ask_wheel(label: str) -> str:
    """Return 'play' or 'skip'."""
    while True:
        try:
            raw = input(
                f"  {cyan('>>')} Play the {label}? {dim('(y = play  n = skip)')}: "
            ).strip().lower()
        except EOFError:
            return "skip"
        if raw in ("y", "yes"):
            return "play"
        if raw in ("n", "no"):
            return "skip"
        warn("Please answer y or n.")


def browser_click(
    page_url: str,
    selector: str,
    wait_ms: int,
    screenshot: str,
    user_agent: Optional[str],
) -> Tuple[bool, Dict[str, str], str]:
    """Click a popup control in a real browser and return its cookies.

    The popup controls are plain <div> elements and the wheel is drawn on a
    <canvas>, so no request can be replayed from the markup alone; a real
    browser dispatches the click event the page's JavaScript listens for.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, {}, (
            "Playwright is not installed. Run:  pip install playwright  then  "
            "playwright install chromium"
        )

    cookies: Dict[str, str] = {}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=user_agent
                or (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                )
            )
            page = context.new_page()
            page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_selector(selector, timeout=8000)
            page.click(selector)
            page.wait_for_timeout(max(0, wait_ms))
            if screenshot:
                try:
                    page.screenshot(path=screenshot)
                except Exception:
                    pass
            for cookie in context.cookies():
                cookies[cookie["name"]] = cookie["value"]
            browser.close()
        return True, cookies, ""
    except Exception as exc:
        return False, {}, str(exc)


def handle_wheel(
    session: requests.Session,
    cfg: SiteConfig,
    html: str,
    page_url: str,
    mode: str,
    use_browser: bool,
    dry_run: bool,
    user_agent: Optional[str],
    label: str,
    screenshot: str,
) -> str:
    """Return one of: 'absent' | 'played' | 'skipped' | 'declined' | 'dry-run'."""
    if not cfg.wheel.enabled:
        info("Wheel disabled in config.")
        return "absent"

    presence = detect_wheel(html, cfg.wheel)

    # Popup not in the raw HTML: in browser+play mode it may be JS-rendered.
    if not presence.found:
        if use_browser and mode == "play" and not dry_run:
            info(f"{label} not in the raw HTML - trying the browser (JS-rendered popup).")
            clicked, cookies, problem = browser_click(
                page_url, cfg.wheel.go_selector, cfg.wheel.wait_ms, screenshot, user_agent
            )
            if clicked:
                for name, value in cookies.items():
                    session.cookies.set(name, value)
                ok(f"{label} spun; screenshot: {screenshot}")
                return "played"
            info(f"{label} not found: {problem}")
            return "absent"
        info(f"No {label} popup detected on this page.")
        return "absent"

    ok(f"{label} popup detected")
    if presence.title:
        kv("banner", presence.title)
    kv("GO (y)", f"div{presence.go_selector}")
    kv("dismiss (n)", f"div{presence.skip_selector}")

    if mode == "ask":
        choice = ask_wheel(label)
    else:
        choice = "play" if mode == "play" else "skip"
        info(f"Answer: {'y' if choice == 'play' else 'n'} (mode: {mode}).")

    if dry_run:
        warn(f"Dry run: the {label} would be {'spun' if choice == 'play' else 'dismissed'}.")
        return "dry-run"

    if choice == "skip":
        info(f"Answer: n - dismissing the popup via div{presence.skip_selector}.")
        if use_browser:
            clicked, cookies, problem = browser_click(
                page_url, cfg.wheel.skip_selector, 600, "", user_agent
            )
            if clicked:
                for name, value in cookies.items():
                    session.cookies.set(name, value)
                ok("Popup dismissed in the browser.")
                return "skipped"
            warn(f"Could not dismiss the popup: {problem}")
        else:
            info("Continuing (the popup blocks nothing server-side).")
        return "skipped"

    info(f"Answer: y - spinning via div{presence.go_selector}.")
    if use_browser:
        clicked, cookies, problem = browser_click(
            page_url, cfg.wheel.go_selector, cfg.wheel.wait_ms, screenshot, user_agent
        )
        if clicked:
            for name, value in cookies.items():
                session.cookies.set(name, value)
            ok(f"Wheel spun; result screenshot: {screenshot}")
            info("Browser cookies were carried over to the session.")
            return "played"
        warn(f"Could not spin the wheel: {problem}")
        return "declined"

    warn("The wheel is drawn on a <canvas> and GO is a plain <div> with no URL,")
    warn("href or form action, so the spin cannot be replayed over plain HTTP.")
    info("Re-run with --browser to click it in a real browser.")
    return "declined"


# ----------------------------------------------------------------------------
# Result evaluation
# ----------------------------------------------------------------------------


def evaluate(resp, cfg: SiteConfig, use_default_scan: bool) -> Tuple[bool, str]:
    rule = cfg.success
    if resp.status_code not in rule.status:
        return False, f"HTTP {resp.status_code} (expected one of {rule.status})"
    body = (resp.text or "").lower()
    for bad in rule.body_not_contains:
        if bad.lower() in body:
            return False, f"the site reported: {bad!r}"
    for good in rule.body_contains:
        if good.lower() not in body:
            return False, f"expected marker {good!r} not found in the response"
    for frag in rule.redirect_contains:
        if frag.lower() not in resp.url.lower():
            return False, f"final URL {resp.url} does not contain {frag!r}"
    if use_default_scan:
        for kw in DEFAULT_ERROR_KEYWORDS:
            if kw in body:
                return False, f"the page shows an error: {kw!r}"
    return True, "registration accepted"


def scan_errors(html: str) -> List[str]:
    body = (html or "").lower()
    return [kw for kw in DEFAULT_ERROR_KEYWORDS if kw in body]


# ----------------------------------------------------------------------------
# Prompts
# ----------------------------------------------------------------------------


def normalize_mobile(raw: str) -> str:
    return re.sub(r"[\s\-().]", "", (raw or "").strip())


def validate_mobile(value: str, length: int) -> Tuple[bool, str]:
    if not value:
        return False, "empty number"
    if not value.isdigit():
        return False, "digits only, please"
    if length and len(value) != length:
        return False, f"expected exactly {length} digits, got {len(value)}"
    return True, ""


def ask_mobile(prefill: Optional[str], length: int) -> str:
    if prefill:
        value = normalize_mobile(prefill)
        good, why = validate_mobile(value, length)
        if not good:
            err(f"Invalid mobile number: {why}")
            raise SystemExit(2)
        return value
    while True:
        try:
            raw = input(f"  {cyan('>>')} Mobile number {dim(f'({length} digits)')}: ").strip()
        except EOFError:
            err("No input available.")
            raise SystemExit(2)
        value = normalize_mobile(raw)
        good, why = validate_mobile(value, length)
        if good:
            return value
        warn(why or "Please type a valid mobile number.")


def ask_otp(can_resend: bool, otp_length: int) -> Optional[str]:
    size_hint = f"{dim(f'({otp_length} characters)')}" if otp_length else ""
    actions = "  r = resend code  q = quit" if can_resend else "  q = quit"
    while True:
        try:
            raw = input(f"  {cyan('>>')} Enter the OTP code {size_hint} {dim(actions)}: ").strip()
        except EOFError:
            return None
        low = raw.lower()
        if low == "q":
            return None
        if low == "r":
            if can_resend:
                return RESEND
            warn("No resend link was found on this page.")
            continue
        if not raw:
            warn("Empty code, try again.")
            continue
        if not re.fullmatch(r"[A-Za-z0-9]{4,12}", raw):
            warn("Codes are usually 4-8 digits/letters - check and retype.")
            continue
        if otp_length and len(raw) != otp_length:
            warn(f"Expected {otp_length} characters, got {len(raw)}.")
            continue
        return raw


# ----------------------------------------------------------------------------
# Resend helper
# ----------------------------------------------------------------------------


def do_resend(
    session: requests.Session,
    cfg: SiteConfig,
    otp_page: PageForms,
    timeout: int,
    use_browser: bool,
    user_agent: Optional[str],
) -> Optional[PageForms]:
    """Ask the site for a new code; return a refreshed OTP page or None."""
    if otp_page.resend_url:
        info(f"Requesting a new code: {otp_page.resend_url}")
        try:
            r = session.get(otp_page.resend_url, timeout=timeout)
            return parse_page(r.text, r.url, cfg)
        except requests.RequestException as exc:
            warn(f"Resend failed: {exc}")
            return None

    page_url = otp_page.otp.page_url if otp_page.otp else cfg.register_url
    if use_browser and cfg.resend_selector:
        info(f"Clicking the resend control in the browser: {cfg.resend_selector}")
        clicked, cookies, problem = browser_click(
            page_url, cfg.resend_selector, 1200, "", user_agent
        )
        if not clicked:
            warn(f"Resend click failed: {problem}")
            return None
        for name, value in cookies.items():
            session.cookies.set(name, value)
        try:
            r = session.get(page_url, timeout=timeout)
            return parse_page(r.text, r.url, cfg)
        except requests.RequestException:
            return None

    warn("The resend control is a plain <button> with no href or form action,")
    warn("and the markup does not show any URL behind it.")
    warn("Re-run with --browser (\"resend_selector\" is set) to click it for real.")
    return None


# ----------------------------------------------------------------------------
# Logging / debug
# ----------------------------------------------------------------------------


def save_record(path: str, record: Dict[str, object]) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _dump(name: str, html: str) -> None:
    with open(name, "w", encoding="utf-8") as fh:
        fh.write(html or "")
    info(f"saved {name}")


def _report_candidates(page: PageForms) -> None:
    if not page.candidates:
        info("no visible form fields were found")
        return
    info("field detection scores (mobile / otp):")
    for name, m, o in page.candidates:
        info(f"  {name.ljust(28)} {str(m).rjust(4)} / {str(o).rjust(4)}")


# ----------------------------------------------------------------------------
# Starter config
# ----------------------------------------------------------------------------


EXAMPLE_CONFIG: Dict[str, object] = {
    "site": "789Bingo",
    "register_url": "https://PUT-THE-SIGNUP-URL-HERE",
    "mobile_field": "phoneNumberOrEmail",
    "mobile_length": 11,
    "otp_field": "sms-code",
    "resend_selector": ".css-hdbbym",
    "send_marker": "Verification code sent successfully",
    "otp_url": "",
    "otp_length": 0,
    "static_fields": {},
    "wheel": {
        "enabled": True,
        "go_selector": ".css-3z4mse",
        "skip_selector": ".css-bwzg6b",
        "title_selector": ".css-c6ko63",
        "title_contains": "Login To Win Big Rewards",
        "replay_url": "",
        "wait_ms": 4500,
        "screenshot": "wheel_welcome.png",
        "screenshot2": "wheel_reward.png"
    },
    "success": {
        "status": [200, 302],
        "body_contains": [],
        "body_not_contains": [],
        "redirect_contains": []
    },
    "headers": {},
    "timeout": 20
}


def write_example_config() -> int:
    path = EXAMPLE_CONFIG_NAME
    if os.path.exists(path):
        warn(f"{path} already exists - not overwriting.")
        return 1
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(EXAMPLE_CONFIG, fh, indent=2)
        fh.write("\n")
    ok(f"Wrote {path}")
    info("1. Put the signup page URL in \"register_url\".")
    info("2. Field names are already wired to the 789Bingo forms")
    info("   (phoneNumberOrEmail / sms-code / .css-hdbbym).")
    info("3. Test with:  python register.py --config " + path + " --dry-run")
    return 0


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

EPILOG = """examples:
  python register.py --init
  python register.py --config site_config.json --dry-run
  python register.py --config site_config.json
  python register.py --config site_config.json --wheel play --wheel2 play --browser
  python register.py --config site_config.json --mobile "09171234567"

flow (as specified):
  1. welcome wheel popup  -> answer y (play) or n (skip)
  2. enter a 10-digit mobile number
  3. enter the OTP code received by SMS
  4. spin the wheel again (reward spin)

wheel popup (from the provided markup):
  y = GO control     div.css-3z4mse
  n = dismiss        div.css-bwzg6b
  banner text        div.css-c6ko63  "Login To Win Big Rewards"

forms (from the provided markup):
  mobile input       phoneNumberOrEmail
  otp input          sms-code
  resend button      .css-hdbbym  "Resend verification code"
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="register.py",
        description="Wheel -> 10-digit mobile -> OTP -> wheel again (authorized targets only).",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--config", "-c", default=DEFAULT_CONFIG, help="path to site config JSON")
    p.add_argument("--init", action="store_true", help=f"write {EXAMPLE_CONFIG_NAME} and exit")
    p.add_argument("--mobile", help="your mobile number (skips the prompt)")
    p.add_argument("--otp", help="the OTP code (skips the prompt; for testing)")
    p.add_argument("--otp-length", type=int, default=None, help="expected OTP length (overrides config)")
    p.add_argument("--mobile-length", type=int, default=None, help="expected mobile length (default 10)")
    p.add_argument("--attempts", type=int, default=3, help="max OTP submissions before giving up")
    p.add_argument("--wheel", choices=["ask", "play", "skip"], default="ask",
                   help="welcome wheel: ask (default), play, or skip")
    p.add_argument("--wheel2", choices=["ask", "play", "skip"], default="play",
                   help="reward wheel after registration: play (default), ask, or skip")
    p.add_argument("--browser", action="store_true",
                   help="use Playwright to actually click the wheel/resend controls (pip install playwright)")
    p.add_argument("--timeout", type=int, default=20)
    p.add_argument("--dry-run", action="store_true", help="show the payload but do not send anything")
    p.add_argument("--debug", action="store_true", help="save response HTML and show field-detection scores")
    p.add_argument("--out", default=DEFAULT_LOG, help="JSONL log file")
    p.add_argument("--no-log", action="store_true", help="do not write a log file")
    p.add_argument("--user-agent", default=None)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    enable_windows_ansi()
    args = build_parser().parse_args(argv)
    banner()

    if args.init:
        return write_example_config()

    if args.attempts < 1:
        err("--attempts must be >= 1")
        return 2

    try:
        cfg = SiteConfig.load(args.config)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        err(str(exc))
        return 2

    otp_length = args.otp_length if args.otp_length is not None else cfg.otp_length
    mobile_length = args.mobile_length if args.mobile_length is not None else cfg.mobile_length
    user_agent = args.user_agent or (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
    use_browser = args.browser and not args.dry_run

    # -- 1. load page ------------------------------------------------------
    step(1, 6, f"Loading registration page - {bold(cfg.site)}")
    kv("url", cfg.register_url)

    session = requests.Session()
    session.headers.update({
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    session.headers.update(cfg.headers)

    try:
        resp = session.get(cfg.register_url, timeout=args.timeout)
        resp.raise_for_status()
    except requests.RequestException as exc:
        err(f"Could not load the page: {exc}")
        return 1

    if args.debug:
        _dump("debug_01_register.html", resp.text)

    page = parse_page(resp.text, resp.url, cfg)
    ok(f"Page loaded: {page.title or resp.url}")
    if args.debug:
        _report_candidates(page)

    # -- 2. welcome wheel popup -------------------------------------------
    step(2, 6, "Welcome bonus wheel popup")
    wheel_welcome = handle_wheel(
        session, cfg, resp.text, resp.url, args.wheel,
        use_browser, args.dry_run, user_agent,
        "welcome wheel", cfg.wheel.screenshot,
    )
    kv("welcome wheel", wheel_welcome)

    if page.mobile is None:
        err("No mobile-number field found on the page.")
        info("Set \"mobile_field\" in the config to the exact input name.")
        names = ", ".join(n for n, _, _ in page.candidates) or "(none)"
        info(f"Visible fields seen: {names}")
        return 1
    kv("mobile field", page.mobile.field)

    # -- 3. mobile number -------------------------------------------------
    step(3, 6, f"Your mobile number {dim(f'({mobile_length} digits)')}")
    mobile = ask_mobile(args.mobile, mobile_length)
    kv("number", mobile)

    # -- 4. send mobile number + find OTP form ----------------------------
    step(4, 6, "Sending your mobile number")
    kv("target", f"{page.mobile.method} {page.mobile.action}")

    preview = dict(page.mobile.payload)
    preview.update(cfg.static_fields)
    preview[page.mobile.field] = mobile
    for key in sorted(preview):
        kv(key, preview[key][:80])

    if args.dry_run:
        warn("Dry run: nothing was sent. Exiting.")
        return 0

    try:
        resp2 = _submit(session, page.mobile, args.timeout, cfg.static_fields,
                        {page.mobile.field: mobile})
    except requests.RequestException as exc:
        err(f"Could not submit the form: {exc}")
        return 1
    ok(f"Form submitted (HTTP {resp2.status_code})")

    if cfg.send_marker:
        if cfg.send_marker.lower() in (resp2.text or "").lower():
            ok(f"Send marker found: {cfg.send_marker!r}")
        else:
            warn(f"Send marker {cfg.send_marker!r} not seen (JS-rendered dialogs may hide it).")

    if cfg.otp_url:
        otp_src = urljoin(cfg.register_url, cfg.otp_url)
        info(f"Loading OTP page from config: {otp_src}")
        try:
            resp2 = session.get(otp_src, timeout=args.timeout)
        except requests.RequestException as exc:
            err(f"Could not load the OTP page: {exc}")
            return 1

    if args.debug:
        _dump("debug_02_otp.html", resp2.text)

    otp_page = parse_page(resp2.text, resp2.url, cfg)
    if otp_page.otp is None:
        err("No OTP entry field found after sending the number.")
        found = scan_errors(resp2.text)
        if found:
            warn("The page mentions: " + ", ".join(found))
        info("If the OTP form is rendered by JavaScript or lives at another URL,")
        info("set \"otp_url\" / \"otp_field\" in the config, or use --browser.")
        return 1
    ok(f"OTP form found (field: {otp_page.otp.field})")
    carry_mobile(otp_page.otp, page.mobile.field, mobile)
    if otp_page.resend_url or cfg.resend_selector:
        info("Resend available: " + (otp_page.resend_url or cfg.resend_selector))

    # -- 5. enter + submit the OTP ----------------------------------------
    step(5, 6, "Waiting for your OTP")
    info(f"The site should send an SMS code to {bold(mobile)}.")
    info("Type the code below when it arrives.")

    submitted = 0
    success = False
    detail = "no code submitted"
    last_resp = None
    otp_target = otp_page.otp
    can_resend = bool(otp_page.resend_url or cfg.resend_selector)
    use_default_scan = not (
        cfg.success.body_contains
        or cfg.success.body_not_contains
        or cfg.success.redirect_contains
    )

    while True:
        code = args.otp if (args.otp and submitted == 0) else ask_otp(can_resend, otp_length)

        if code is None:
            warn("Cancelled.")
            return 130

        if code == RESEND:
            refreshed = do_resend(session, cfg, otp_page, args.timeout, use_browser, user_agent)
            if refreshed is not None:
                otp_page = refreshed
                if otp_page.otp:
                    otp_target = otp_page.otp
                    carry_mobile(otp_target, page.mobile.field, mobile)
                ok("Resend requested. Check your phone again.")
            continue

        info(f"Submitting code {bold(code)} -> {otp_target.method} {otp_target.action}")
        try:
            resp3 = _submit(session, otp_target, args.timeout, cfg.static_fields,
                            {otp_target.field: code})
        except requests.RequestException as exc:
            err(f"Could not submit the code: {exc}")
            return 1
        last_resp = resp3

        if args.debug:
            _dump(f"debug_03_result_{submitted + 1}.html", resp3.text)

        success, detail = evaluate(resp3, cfg, use_default_scan)
        submitted += 1

        if success:
            ok(detail)
            break

        warn(detail)
        if submitted >= args.attempts:
            err(f"Giving up after {submitted} attempt(s).")
            break

        # refresh the form: failed attempts usually rotate the CSRF token
        otp_page = parse_page(resp3.text, resp3.url, cfg)
        if otp_page.otp:
            otp_target = otp_page.otp
            carry_mobile(otp_target, page.mobile.field, mobile)
        else:
            warn("OTP form not found on the error page; reusing the previous form.")

    # -- 6. spin the wheel again ------------------------------------------
    step(6, 6, "Reward wheel spin")
    wheel_reward = "absent"
    if success:
        reward_html = last_resp.text if last_resp is not None else ""
        reward_url = getattr(last_resp, "url", None) or resp.url
        if cfg.wheel.replay_url:
            reward_url = urljoin(cfg.register_url, cfg.wheel.replay_url)
            info(f"Loading the reward wheel page: {reward_url}")
            try:
                r = session.get(reward_url, timeout=args.timeout)
                reward_html, reward_url = r.text, r.url
            except requests.RequestException as exc:
                warn(f"Could not load the reward wheel page: {exc}")
        if args.debug:
            _dump("debug_04_reward.html", reward_html)
        wheel_reward = handle_wheel(
            session, cfg, reward_html, reward_url, args.wheel2,
            use_browser, args.dry_run, user_agent,
            "reward wheel", cfg.wheel.screenshot2,
        )
    else:
        info("Registration did not succeed - skipping the reward spin.")
    kv("reward wheel", wheel_reward)

    record = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "site": cfg.site,
        "wheel_welcome": wheel_welcome,
        "mobile": mobile,
        "attempts": submitted,
        "success": success,
        "detail": detail,
        "wheel_reward": wheel_reward,
        "http_status": getattr(last_resp, "status_code", None),
        "final_url": getattr(last_resp, "url", None),
    }
    if not args.no_log:
        save_record(args.out, record)

    print()
    line = "+" + "-" * 58 + "+"
    print(cyan(line))
    label = "SUCCESS" if success else "FAILED "
    frame_line(f"  {label}  {detail}", code="1;32" if success else "1;31")
    frame_line(f"  wheel: {wheel_welcome} -> {wheel_reward}")
    frame_line(f"  log: {args.out if not args.no_log else '(disabled)'}")
    print(cyan(line))
    return 0 if success else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print()
        warn("Cancelled.")
        raise SystemExit(130)