"""Self-service setup page (`/setup`).

A tiny WSGI app, mounted alongside the WebDAV server, that walks a user
through logging in to Immich (password or SSO), mints a narrowly-scoped API
key for them, and hands back a ready-to-paste PowerShell script that sets up
the rclone mount.

All Immich calls are made server-side against ``IMMICH_EXTERNAL_URL`` (a real
public URL) -- never from browser JS, which would hit Immich cross-origin.

The response that carries the freshly minted key is sent ``Cache-Control:
no-store`` and the key is only ever shown as the immediate result of a
just-completed login (no separate, fetchable "download the script" URL).
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import re
import secrets
import threading
import time
from typing import Any
from urllib.parse import parse_qs

import requests

logger = logging.getLogger(__name__)

# Exactly these -- nothing broader. No `user.read`: the server only lists
# albums and fetches asset bytes/thumbnails, it never reads user info (the
# old GET /api/users/me auth check is gone).
_KEY_PERMISSIONS = ["album.read", "asset.read", "asset.view", "asset.download"]
_KEY_NAME = "immich-webdav (rclone mount)"

_TXN_TTL_SECONDS = 600.0
_MAX_BODY_BYTES = 64 * 1024
# Permissive, but with no whitespace and no `'` so it can't break out of the
# single-quoted `$key = '...'` in the generated PowerShell.
_SECRET_RE = re.compile(r"\A[^\s']{8,512}\Z")

# rclone mount options exposed in the /setup "Advanced" section. Each value is
# whitelist-validated (no quotes, no shell metacharacters) before it goes into
# the generated PowerShell (single-quoted string) and VBS (double-quoted).
_MOUNT_DEFAULTS = {
    "drive": "I:", "mode": "full", "size": "50G", "age": "8760h",
    "dirtime": "5s", "volname": "immich", "extra": "",
}
_MOUNT_RULES = {
    "drive": re.compile(r"\A[A-Za-z]:\Z"),
    "mode": re.compile(r"\A(off|minimal|writes|full)\Z"),
    "size": re.compile(r"\A(off|\d+([KMGTP]i?B?)?)\Z"),
    "age": re.compile(r"\A(0|(\d+(ns|us|ms|s|m|h|d|w|y))+)\Z"),
    "dirtime": re.compile(r"\A(0|(\d+(ns|us|ms|s|m|h|d|w|y))+)\Z"),
    "volname": re.compile(r"\A[A-Za-z0-9 ._-]{1,40}\Z"),
    "extra": re.compile(r"\A[A-Za-z0-9 =:_./,-]{0,256}\Z"),
}
_MOUNT_LABELS = {
    "drive": "Drive letter", "mode": "VFS cache mode", "size": "VFS cache max size",
    "age": "VFS cache max age", "dirtime": "Dir cache time",
    "volname": "Volume label", "extra": "Extra rclone flags",
}


def _validate_mount_opts(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raw = {}
    opts = dict(_MOUNT_DEFAULTS)
    for key, rule in _MOUNT_RULES.items():
        val = str(raw.get(key, "") or "").strip() or _MOUNT_DEFAULTS[key]
        if not rule.match(val):
            raise _SetupError(400, f"Advanced: {_MOUNT_LABELS[key]!r} isn't a valid value.")
        opts[key] = val
    opts["drive"] = opts["drive"].upper()
    return opts

# (method, path) -> handler method name. Shared with server.py's dispatcher so
# only these exact routes are stolen from the WebDAV app. WebDAV clients never
# `GET /` (they PROPFIND/OPTIONS it), so serving the page there is safe; and an
# album literally named "setup" is still reachable via PROPFIND etc. The
# `/setup/*` sub-routes keep their own namespace (and the OAuth redirect URI
# `/setup/callback` stays stable regardless of where the page lives).
_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/"): "_page",
    ("GET", "/setup/auth-mode"): "_auth_mode",
    ("POST", "/setup/login"): "_password_login",
    ("POST", "/setup/oauth/start"): "_oauth_start",
    ("GET", "/setup/callback"): "_oauth_callback",
}
SETUP_ROUTE_KEYS = frozenset(_ROUTES)

_REASON = {
    200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found",
    405: "Method Not Allowed", 429: "Too Many Requests",
    500: "Internal Server Error", 502: "Bad Gateway",
}


class _SetupError(Exception):
    """An expected, user-facing failure (bad password, expired link, ...)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class SetupApp:
    def __init__(
        self,
        *,
        immich_external_url: str,
        service_external_url: str,
        request_timeout_seconds: float,
    ) -> None:
        self._immich = immich_external_url.rstrip("/")
        self._service_override = service_external_url.rstrip("/")
        self._timeout = request_timeout_seconds
        # state -> (created_monotonic, code_verifier, redirect_uri, mount_opts)
        self._txns: dict[str, tuple] = {}
        self._lock = threading.Lock()

    # -- WSGI ------------------------------------------------------------

    def __call__(self, environ: dict[str, Any], start_response):
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "") or "/"

        handler_name = _ROUTES.get((method, path))
        if handler_name is None:
            if any(path == p for _m, p in _ROUTES):
                return self._send(start_response, 405, "text/plain", b"Method not allowed")
            return self._send(start_response, 404, "text/plain", b"Not found")

        is_navigation = path == "/setup/callback"
        try:
            return getattr(self, handler_name)(environ, start_response)
        except _SetupError as exc:
            return self._fail(start_response, exc.status, exc.message, is_navigation)
        except requests.RequestException:
            logger.exception("setup: Immich request failed (%s)", path)
            return self._fail(
                start_response, 502,
                "Could not reach Immich, or it rejected the request. "
                "Check IMMICH_EXTERNAL_URL and try again.",
                is_navigation,
            )
        except Exception:
            logger.exception("setup: unhandled error on %s", path)
            return self._fail(start_response, 500, "Internal error.", is_navigation)

    # -- routes --------------------------------------------------------

    def _page(self, environ, start_response):
        return self._send(start_response, 200, "text/html; charset=utf-8", _page_html())

    def _auth_mode(self, environ, start_response):
        resp = requests.get(f"{self._immich}/api/server/features", timeout=self._timeout)
        resp.raise_for_status()
        password_login = bool(resp.json().get("passwordLogin", True))
        return self._json(start_response, 200, {"passwordLogin": password_login})

    def _password_login(self, environ, start_response):
        body = _read_json(environ)
        email = str(body.get("email") or "").strip()
        password = str(body.get("password") or "")
        if not email or not password:
            raise _SetupError(400, "Email and password are required.")
        opts = _validate_mount_opts(body.get("opts"))

        client_ip = _client_ip(environ)
        resp = requests.post(
            f"{self._immich}/api/auth/login",
            json={"email": email, "password": password},
            # Pass the real client IP through so Immich's own login throttle
            # sees per-attacker addresses, not just this container's.
            headers={"X-Forwarded-For": client_ip} if client_ip != "?" else {},
            timeout=self._timeout,
        )
        if resp.status_code == 401:
            # One line per rejected attempt, with the real client IP, for a
            # log-based fail2ban / CrowdSec scenario (this endpoint is an
            # unauthenticated proxy, so it's the only place that signal exists).
            logger.warning("setup: login rejected ip=%s", client_ip)
            raise _SetupError(401, "Incorrect email or password.")
        if resp.status_code == 429:
            logger.warning("setup: login throttled ip=%s", client_ip)
            raise _SetupError(429, "Too many attempts. Wait a few minutes and try again.")
        resp.raise_for_status()
        token = _require(resp.json(), "accessToken")
        return self._json(start_response, 200, {"script": self._mint(token, environ, opts)})

    def _oauth_start(self, environ, start_response):
        try:
            body = _read_json(environ)
        except _SetupError:
            body = {}
        opts = _validate_mount_opts(body.get("opts"))

        redirect_uri = f"{self._service_url(environ)}/setup/callback"
        verifier = _b64url(secrets.token_bytes(32))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        state = secrets.token_urlsafe(24)

        with self._lock:
            self._prune_locked()
            self._txns[state] = (time.monotonic(), verifier, redirect_uri, opts)

        resp = requests.post(
            f"{self._immich}/api/oauth/authorize",
            json={"codeChallenge": challenge, "redirectUri": redirect_uri, "state": state},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return self._json(start_response, 200, {"url": _require(resp.json(), "url")})

    def _oauth_callback(self, environ, start_response):
        query = environ.get("QUERY_STRING", "")
        state = parse_qs(query).get("state", [""])[0]
        with self._lock:
            self._prune_locked()
            txn = self._txns.pop(state, None) if state else None
        if txn is None:
            raise _SetupError(400, "This sign-in link has expired. Start over at /setup.")
        _created, verifier, _redirect_uri, opts = txn

        full_url = f"{self._service_url(environ)}/setup/callback"
        if query:
            full_url = f"{full_url}?{query}"

        resp = requests.post(
            f"{self._immich}/api/oauth/callback",
            json={"url": full_url, "codeVerifier": verifier, "state": state},
            timeout=self._timeout,
        )
        if resp.status_code >= 400:
            raise _SetupError(400, "SSO sign-in failed. Start over at /setup.")
        token = _require(resp.json(), "accessToken")
        return self._send(
            start_response, 200, "text/html; charset=utf-8",
            _result_page(self._mint(token, environ, opts)),
        )

    # -- shared -------------------------------------------------------

    def _mint(self, access_token: str, environ, opts: dict) -> str:
        resp = requests.post(
            f"{self._immich}/api/api-keys",
            json={"name": _KEY_NAME, "permissions": _KEY_PERMISSIONS},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        secret = _require(resp.json(), "secret")
        if not _SECRET_RE.match(secret):
            raise _SetupError(502, "Immich returned an unexpected API key format.")
        return _powershell_script(secret, self._service_url(environ), opts)

    def _service_url(self, environ) -> str:
        if self._service_override:
            return self._service_override
        proto = (
            environ.get("HTTP_X_FORWARDED_PROTO")
            or environ.get("wsgi.url_scheme")
            or "https"
        ).split(",")[0].strip()
        host = (
            environ.get("HTTP_X_FORWARDED_HOST") or environ.get("HTTP_HOST") or ""
        ).split(",")[0].strip()
        if not host:
            raise _SetupError(
                500, "Cannot determine this service's external URL. Set SERVICE_EXTERNAL_URL."
            )
        return f"{proto}://{host}"

    def _prune_locked(self) -> None:
        cutoff = time.monotonic() - _TXN_TTL_SECONDS
        for state in [s for s, t in self._txns.items() if t[0] < cutoff]:
            self._txns.pop(state, None)

    def _fail(self, start_response, status: int, message: str, navigation: bool):
        if navigation:
            return self._send(start_response, status, "text/html; charset=utf-8",
                              _error_page(message))
        return self._json(start_response, status, {"error": message})

    def _json(self, start_response, status: int, payload: dict):
        return self._send(start_response, status, "application/json",
                          json.dumps(payload).encode("utf-8"))

    def _send(self, start_response, status: int, content_type: str, body: bytes):
        start_response(
            f"{status} {_REASON.get(status, 'OK')}",
            [
                ("Content-Type", content_type),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
                ("Referrer-Policy", "no-referrer"),
                ("X-Content-Type-Options", "nosniff"),
            ],
        )
        return [body]


# -- helpers ---------------------------------------------------------------


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _client_ip(environ) -> str:
    """Best-effort real client IP (first `X-Forwarded-For` hop, else peer)."""
    xff = environ.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[0].strip()
    return environ.get("HTTP_X_REAL_IP") or environ.get("REMOTE_ADDR") or "?"


def _read_json(environ) -> dict:
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length <= 0 or length > _MAX_BODY_BYTES:
        raise _SetupError(400, "Missing or oversized request body.")
    raw = environ["wsgi.input"].read(length)
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise _SetupError(400, "Request body must be JSON.") from exc
    if not isinstance(data, dict):
        raise _SetupError(400, "Request body must be a JSON object.")
    return data


def _require(payload: Any, key: str) -> str:
    if isinstance(payload, dict) and isinstance(payload.get(key), str) and payload[key]:
        return payload[key]
    raise _SetupError(502, f"Immich response was missing {key!r}.")


# One physical line so it pastes into the PowerShell console and runs atomically
# (a multi-line block executes line-by-line as it's pasted and mis-fires on the
# `if (...) {` blocks). @@URL@@ / @@KEY@@ / @@DRIVE@@ / @@FLAGS@@ are filled in
# per request (all pre-validated -- see _validate_mount_opts).
#
# The mount runs from a tiny mount.vbs via wscript.exe -- a GUI-subsystem host
# with no console -- because `powershell -WindowStyle Hidden` / `rclone
# --no-console` still leave a window on some machines, and closing it kills the
# mount. The scheduled task re-runs at every logon (that's the crash recovery).
_PS_ONELINER = (
    r"""[Net.ServicePointManager]::SecurityProtocol='Tls12'; $ProgressPreference='SilentlyContinue'; """
    r"""$d="$env:LOCALAPPDATA\immich-webdav"; New-Item -Type Directory -Force $d | Out-Null; """
    r"""$rc=(Get-Command rclone -ErrorAction Ignore).Source; if(-not $rc){$rc="$d\rclone.exe"}; """
    r"""if(-not (Test-Path $rc)){$z="$env:TEMP\rc-immich.zip"; $x="$env:TEMP\rc-immich"; """
    r"""Invoke-WebRequest 'https://downloads.rclone.org/rclone-current-windows-amd64.zip' -OutFile $z; """
    r"""Expand-Archive $z $x -Force; Copy-Item (Get-ChildItem $x -Recurse -Filter rclone.exe)[0].FullName $rc -Force; """
    r"""Remove-Item $z,$x -Recurse -Force}; """
    r"""if(-not (Get-Service WinFsp.Launcher -ErrorAction Ignore)){"""
    r"""if(-not (Get-Command winget -ErrorAction Ignore)){throw 'Install WinFsp from https://winfsp.dev/ then re-run'}; """
    r"""winget install --id WinFsp.WinFsp --exact --source winget --accept-package-agreements --accept-source-agreements; """
    r"""if(-not (Get-Service WinFsp.Launcher -ErrorAction Ignore)){throw 'WinFsp install failed -- install it from https://winfsp.dev/ then re-run'}}; """
    r"""& $rc config create immich-web webdav url=@@URL@@ vendor=other user=anyuser pass=$(& $rc obscure '@@KEY@@') | Out-Null; """
    r"""$fl='@@FLAGS@@'; """
    r"""$vbs="$d\mount.vbs"; $rce=$rc -replace '"','""'; """
    r"""Set-Content $vbs -Encoding ASCII -Value ('Set s=CreateObject("WScript.Shell") : s.Run Chr(34) & "' + $rce + '" & Chr(34) & " mount immich-web: @@DRIVE@@ ' + $fl + '", 0, False'); """
    r"""Register-ScheduledTask -TaskName 'immich-webdav mount' -Force -User $env:USERNAME -RunLevel Limited """
    r"""-Action (New-ScheduledTaskAction -Execute 'wscript.exe' -Argument ('"' + $vbs + '"')) """
    r"""-Trigger (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME) """
    r"""-Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)) | Out-Null; """
    r"""Start-ScheduledTask -TaskName 'immich-webdav mount'; """
    r"""Write-Host 'Mounted at @@DRIVE@@  --  it reconnects automatically after a reboot.' -ForegroundColor Green"""
)


def _powershell_script(secret: str, service_url: str, opts: dict) -> str:
    # `service_url` is the immich-webdav wrapper's own address -- rclone speaks
    # WebDAV to it, not to Immich. All `opts` values are pre-validated.
    # --read-only is not optional: the whole gateway is read-only.
    flags = (
        f"--vfs-cache-mode {opts['mode']} --vfs-cache-max-size {opts['size']} "
        f"--vfs-cache-max-age {opts['age']} --dir-cache-time {opts['dirtime']} "
        f"--read-only --no-console --volname {opts['volname']}"
    )
    if opts["extra"]:
        flags += f" {opts['extra']}"
    return (
        _PS_ONELINER
        .replace("@@URL@@", service_url)
        .replace("@@DRIVE@@", opts["drive"])
        .replace("@@FLAGS@@", flags)
        .replace("@@KEY@@", secret)  # last -- the secret is opaque
    )


_STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; min-height: 100vh; display: grid; place-items: center;
  background: #f4f4f5; font: 15px/1.5 system-ui, -apple-system, Segoe UI, Roboto, sans-serif; }
@media (prefers-color-scheme: dark) { body { background: #18181b; color: #e4e4e7; } }
.card { width: min(92vw, 460px); background: Canvas; color: CanvasText;
  border: 1px solid color-mix(in srgb, CanvasText 12%, transparent);
  border-radius: 14px; padding: 28px; box-shadow: 0 8px 40px rgba(0,0,0,.10); }
h1 { font-size: 1.15rem; margin: 0 0 4px; }
p.sub { margin: 0 0 20px; opacity: .7; font-size: .9rem; }
.tabs { display: flex; gap: 8px; margin-bottom: 18px; }
.tabs button { flex: 1; padding: 8px; border-radius: 9px; cursor: pointer;
  border: 1px solid color-mix(in srgb, CanvasText 18%, transparent); background: transparent; color: inherit; }
.tabs button[aria-selected="true"] { background: #2563eb; border-color: #2563eb; color: #fff; }
label { display: block; font-size: .82rem; opacity: .8; margin: 12px 0 4px; }
input { width: 100%; padding: 9px 11px; border-radius: 9px; font: inherit; color: inherit;
  background: color-mix(in srgb, CanvasText 4%, Canvas);
  border: 1px solid color-mix(in srgb, CanvasText 22%, transparent); }
button.primary { width: 100%; margin-top: 18px; padding: 10px; border: 0; border-radius: 9px;
  background: #2563eb; color: #fff; font: inherit; font-weight: 600; cursor: pointer; }
button.primary:disabled { opacity: .55; cursor: progress; }
.err { margin-top: 12px; color: #dc2626; font-size: .86rem; min-height: 1.2em; }
.hidden { display: none; }
details.adv { margin-top: 16px; font-size: .9rem; }
details.adv summary { cursor: pointer; opacity: .75; }
details.adv .grid { margin-top: 8px; }
details.adv label { display: block; }
.hint { display: block; font-size: .76rem; opacity: .6; margin-top: 3px; }
.hint code { font-size: inherit; background: color-mix(in srgb, CanvasText 8%, transparent);
  padding: 0 3px; border-radius: 4px; }
select { width: 100%; padding: 9px 11px; border-radius: 9px; font: inherit; color: inherit;
  background: color-mix(in srgb, CanvasText 4%, Canvas);
  border: 1px solid color-mix(in srgb, CanvasText 22%, transparent); }
pre { white-space: pre-wrap; word-break: break-word; font: 12.5px/1.5 ui-monospace, Menlo, Consolas, monospace;
  background: color-mix(in srgb, CanvasText 6%, Canvas); padding: 16px; border-radius: 10px;
  border: 1px solid color-mix(in srgb, CanvasText 14%, transparent); max-height: 60vh; overflow: auto; }
.note { font-size: .85rem; opacity: .75; }
""".strip()


_PAGE_BODY = r"""
<main class="card" id="card">
  <h1>Connect your photo drive</h1>
  <p class="sub">Sign in to Immich once. You'll get a PowerShell script that sets up the mount.</p>

  <div class="tabs" role="tablist">
    <button id="tab-password" role="tab" aria-selected="true">Password</button>
    <button id="tab-sso" role="tab" aria-selected="false">SSO</button>
  </div>

  <form id="password-form">
    <label for="email">Email</label>
    <input id="email" type="email" autocomplete="username" required>
    <label for="password">Password</label>
    <input id="password" type="password" autocomplete="current-password" required>
    <button class="primary" type="submit">Sign in &amp; generate script</button>
  </form>

  <p id="password-disabled" class="note hidden">
    Password sign-in is disabled on this Immich. Use SSO.
  </p>

  <div id="sso-pane" class="hidden">
    <p class="note">You'll be redirected to your identity provider and back here.</p>
    <button class="primary" id="sso-btn" type="button">Log in via SSO</button>
  </div>

  <details class="adv">
    <summary>Advanced — mount options</summary>
    <div class="grid">
      <label>Drive letter
        <input id="o-drive" value="I:">
        <small class="hint">Where the drive appears in Explorer, e.g. <code>I:</code>.</small>
      </label>
      <label>VFS cache mode
        <select id="o-mode">
          <option>off</option><option>minimal</option><option>writes</option>
          <option selected>full</option>
        </select>
        <small class="hint"><code>full</code> caches whole files to disk (needed for reliable copies into other apps). <code>off</code> streams; <code>minimal</code>/<code>writes</code> are in between.</small>
      </label>
      <label>VFS cache max size
        <input id="o-size" value="50G">
        <small class="hint">Disk budget for the local file cache, e.g. <code>50G</code>. <code>off</code> = no limit.</small>
      </label>
      <label>VFS cache max age
        <input id="o-age" value="8760h">
        <small class="hint">Drop cached files unused for this long, e.g. <code>8760h</code> (~1 year). <code>0</code> = keep forever.</small>
      </label>
      <label>Dir cache time
        <input id="o-dirtime" value="5s">
        <small class="hint">How long a folder listing is reused before re-checking Immich, e.g. <code>5s</code>. Higher = fewer requests but slower to notice new photos.</small>
      </label>
      <label>Volume label
        <input id="o-volname" value="immich">
        <small class="hint">Name shown beside the drive letter in Explorer.</small>
      </label>
      <label>Extra rclone flags
        <input id="o-extra" placeholder="e.g. --transfers 8">
        <small class="hint">Appended to <code>rclone mount</code> verbatim. Letters, digits, spaces and <code>= : _ . / , -</code> only.</small>
      </label>
    </div>
  </details>

  <p class="err" id="err"></p>
</main>

<script>
const $ = (id) => document.getElementById(id);
const err = $("err");
const setErr = (m) => { err.textContent = m || ""; };
let passwordDisabled = false;

function showTab(which) {
  const pw = which === "password";
  $("tab-password").setAttribute("aria-selected", pw);
  $("tab-sso").setAttribute("aria-selected", !pw);
  $("password-form").classList.toggle("hidden", !pw || passwordDisabled);
  $("password-disabled").classList.toggle("hidden", !pw || !passwordDisabled);
  $("sso-pane").classList.toggle("hidden", pw);
  setErr("");
}

$("tab-password").onclick = () => showTab("password");
$("tab-sso").onclick = () => showTab("sso");

const mountOpts = () => ({
  drive: $("o-drive").value, mode: $("o-mode").value, size: $("o-size").value,
  age: $("o-age").value, dirtime: $("o-dirtime").value,
  volname: $("o-volname").value, extra: $("o-extra").value,
});

fetch("/setup/auth-mode").then((r) => r.json()).then((d) => {
  passwordDisabled = d.passwordLogin === false;
  showTab("password");
}).catch(() => showTab("password"));

function renderScript(script) {
  const card = $("card");
  card.innerHTML = "";
  const h = document.createElement("h1");
  h.textContent = "Paste this into PowerShell (as Administrator)";
  const p = document.createElement("p");
  p.className = "note";
  p.textContent = "One line — paste it into an elevated PowerShell window and press Enter. Contains a live API key; don't share it.";
  const btn = document.createElement("button");
  btn.className = "primary";
  btn.textContent = "Copy";
  const pre = document.createElement("pre");
  pre.textContent = script;
  btn.onclick = () => navigator.clipboard.writeText(script).then(
    () => { btn.textContent = "Copied"; }, () => { btn.textContent = "Press Ctrl+C"; });
  card.append(h, p, btn, pre);
}

$("password-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = e.target.querySelector("button");
  btn.disabled = true; setErr("");
  try {
    const r = await fetch("/setup/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: $("email").value, password: $("password").value, opts: mountOpts() }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "Sign-in failed.");
    renderScript(d.script);
  } catch (ex) {
    setErr(ex.message); btn.disabled = false;
  }
});

$("sso-btn").addEventListener("click", async () => {
  const btn = $("sso-btn");
  btn.disabled = true; setErr("");
  try {
    const r = await fetch("/setup/oauth/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ opts: mountOpts() }),
    });
    const d = await r.json();
    if (!r.ok || !d.url) throw new Error(d.error || "Could not start SSO.");
    window.location.assign(d.url);
  } catch (ex) {
    setErr(ex.message); btn.disabled = false;
  }
});
</script>
"""


def _doc(body: str) -> bytes:
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<meta name=\"robots\" content=\"noindex\">\n"
        "<link rel=\"icon\" href=\"data:,\">\n"
        "<title>immich-webdav setup</title>\n"
        f"<style>{_STYLE}</style>\n</head>\n<body>\n{body}\n</body>\n</html>\n"
    ).encode("utf-8")


def _page_html() -> bytes:
    return _doc(_PAGE_BODY)


def _result_page(script: str) -> bytes:
    return _doc(
        "<main class=\"card\">\n"
        "  <h1>Paste this into PowerShell (as Administrator)</h1>\n"
        "  <p class=\"note\">One line &mdash; paste it into an elevated PowerShell window "
        "and press Enter. Contains a live API key; don't share it.</p>\n"
        "  <button class=\"primary\" id=\"cp\">Copy</button>\n"
        f"  <pre id=\"s\">{html.escape(script)}</pre>\n"
        "  <script>cp.onclick=()=>navigator.clipboard.writeText(s.textContent)"
        ".then(()=>cp.textContent='Copied',()=>cp.textContent='Press Ctrl+C')</script>\n"
        "</main>"
    )


def _error_page(message: str) -> bytes:
    return _doc(
        "<main class=\"card\">\n"
        "  <h1>Something went wrong</h1>\n"
        f"  <p class=\"err\">{html.escape(message)}</p>\n"
        "  <p class=\"note\"><a href=\"/setup\">Start over</a></p>\n"
        "</main>"
    )
