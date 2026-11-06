"""Auth0 login for the event site, done over plain HTTP so no browser is needed.

The web app is an Auth0 SPA (authorization code + PKCE, public client). We play
the same flow: silent `prompt=none` with the Auth0 session cookie when we have
one, otherwise the Universal Login identifier and password forms.
"""

import asyncio
import base64
import hashlib
import html
import json
import logging
import re
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

SCOPE = "openid profile email"

log = logging.getLogger("auth")


class AuthError(Exception):
    pass


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


# What a plain desktop browser reports. Saying no to WebAuthn keeps Auth0 from offering passkey enrollment.
CAPABILITIES = {"js-available": "true", "webauthn-available": "false",
                "is-brave": "false", "webauthn-platform-available": "false"}


def _form_fields(page: str) -> dict[str, str]:
    """The named inputs of the page's first form, which on Auth0's pages is the main one."""
    match = re.search(r"<form[^>]*>(.*?)</form>", page, re.DOTALL)
    fields = {}
    for tag in re.findall(r"<input[^>]*>", match.group(1) if match else ""):
        name = re.search(r'name="([^"]*)"', tag)
        value = re.search(r'value="([^"]*)"', tag)
        if name:
            fields[name.group(1)] = html.unescape(value.group(1)) if value else ""
    return fields


def _page_error(page: str) -> str:
    match = re.search(r'id="error-element-[^"]*"[^>]*>(.*?)<', page, re.DOTALL)
    return html.unescape(match.group(1)).strip() if match else "no error message on page"


def _raise_if_blocked(resp: httpx.Response) -> None:
    """The login domain sits behind Cloudflare, which may challenge or block datacenter IPs."""
    if resp.status_code not in (403, 429, 503):
        return
    if resp.headers.get("cf-mitigated") or "cloudflare" in resp.headers.get("server", "").lower():
        raise AuthError(f"Cloudflare answered HTTP {resp.status_code} at {resp.url.path}; "
                        "set PROXY_MODE=auth, or another Decodo port if the proxy is already on")
    if resp.status_code == 429:
        raise AuthError(f"Auth0 rate-limited the login (HTTP 429) at {resp.url.path}")


class Auth:
    def __init__(self, http: httpx.AsyncClient, auth_url: str, client_id: str, audience: str, site_url: str,
                 email: str, password: str, session_cookie: str, state_dir: Path):
        self.http = http
        self.auth_url = auth_url
        self.client_id = client_id
        self.audience = audience
        self.redirect_uri = site_url
        self.email = email
        self.password = password
        self.state_file = state_dir / "auth.json"
        self.access_token = ""
        self.expires_at = 0.0
        # The launch loops and the watcher share one cookie jar; two logins at once would clobber each other.
        self._lock = asyncio.Lock()
        self._load()
        if session_cookie and "auth0" not in self.http.cookies:
            self.http.cookies.set("auth0", session_cookie, domain=urlparse(auth_url).hostname)

    async def token(self, min_valid_s: float = 600) -> str:
        """A bearer token for the site's API, valid for at least `min_valid_s` more seconds."""
        async with self._lock:
            if self.access_token and self.expires_at - time.time() > min_valid_s:
                return self.access_token
            code, verifier = await self._silent()
            if not code:
                if not (self.email and self.password):
                    raise AuthError("Auth0 session expired and no LOGIN_EMAIL/LOGIN_PASSWORD to log in again")
                log.info("logging in as %s", self.email)
                code, verifier = await self._password_login()
            await self._exchange(code, verifier)
            return self.access_token

    async def _authorize(self, prompt: str | None = None) -> tuple[str, str, httpx.Response]:
        verifier, challenge = _pkce()
        state = secrets.token_urlsafe(16)
        params = {
            "client_id": self.client_id, "scope": SCOPE, "audience": self.audience,
            "redirect_uri": self.redirect_uri,
            "response_type": "code", "state": state, "nonce": secrets.token_urlsafe(16),
            "code_challenge": challenge, "code_challenge_method": "S256",
        }
        if prompt:
            params["prompt"] = prompt
        return verifier, state, await self.http.get(f"{self.auth_url}/authorize", params=params)

    async def _follow(self, resp: httpx.Response, state: str) -> str | httpx.Response:
        """Follows redirects until Auth0 hands back a code (returned) or shows a page (returned)."""
        for _ in range(10):
            _raise_if_blocked(resp)
            if not resp.is_redirect:
                return resp
            location = urljoin(str(resp.url), resp.headers["location"])
            if location.startswith(self.redirect_uri):
                query = parse_qs(urlparse(location).query)
                if "error" in query:
                    raise AuthError(f"{query['error'][0]}: {query.get('error_description', [''])[0]}")
                if query.get("state", [""])[0] != state:
                    raise AuthError("state mismatch in Auth0 redirect")
                if "code" not in query:
                    raise AuthError("Auth0 redirect has no code")
                return query["code"][0]
            if not location.startswith(self.auth_url):
                # An email tied to Google/Microsoft/Facebook is sent off to that provider.
                raise AuthError(f"login left Auth0 for {urlparse(location).netloc}; use AUTH0_SESSION_COOKIE")
            resp = await self.http.get(location)
        raise AuthError("too many redirects")

    async def _silent(self) -> tuple[str | None, str]:
        if "auth0" not in self.http.cookies:
            return None, ""
        verifier, state, resp = await self._authorize(prompt="none")
        try:
            result = await self._follow(resp, state)
        except AuthError as e:
            log.info("silent login failed (%s)", e)
            return None, ""
        return (result, verifier) if isinstance(result, str) else (None, "")

    async def _password_login(self) -> tuple[str, str]:
        verifier, state, resp = await self._authorize()
        result = await self._follow(resp, state)
        if isinstance(result, str):
            return result, verifier
        for _ in range(6):
            path = result.url.path
            fields = _form_fields(result.text)
            if path.endswith("/login/identifier"):
                fields.update(CAPABILITIES, username=self.email)
            elif path.endswith("/login/password"):
                fields.update(username=self.email, password=self.password)
            elif path.endswith("/mfa-detect-browser-capabilities"):
                # A browser's JS fills this in and submits it on its own before Auth0 decides on MFA.
                fields.update(CAPABILITIES)
            else:
                raise AuthError(f"login stopped at {path}: {_page_error(result.text)} (form fields: {sorted(fields)})")
            if "state" not in fields:
                raise AuthError(f"{path} has no state field")
            fields.setdefault("action", "default")
            log.info("login step %s", path)
            result = await self._follow(await self.http.post(str(result.url), data=fields), state)
            if isinstance(result, str):
                return result, verifier
        raise AuthError(f"too many login steps, last at {result.url.path}")

    async def _exchange(self, code: str, verifier: str) -> None:
        resp = await self.http.post(
            f"{self.auth_url}/oauth/token",
            json={"grant_type": "authorization_code", "client_id": self.client_id, "code": code,
                  "code_verifier": verifier, "redirect_uri": self.redirect_uri},
            headers={"origin": self.redirect_uri},
        )
        _raise_if_blocked(resp)
        if resp.status_code != 200:
            raise AuthError(f"token exchange failed: {resp.status_code} {resp.text[:200]}")
        body = resp.json()
        self.access_token = body["access_token"]
        self.expires_at = time.time() + body.get("expires_in", 3600)
        log.info("got token, valid for %d min", body.get("expires_in", 3600) // 60)
        self._save()

    def _load(self) -> None:
        try:
            saved = json.loads(self.state_file.read_text())
            access_token, expires_at = saved["access_token"], float(saved["expires_at"])
            cookies = [(c["name"], c["value"], c["domain"], c["path"]) for c in saved["cookies"]]
        except (OSError, ValueError, KeyError, TypeError) as e:
            if not isinstance(e, FileNotFoundError):
                log.warning("ignoring unreadable %s: %r", self.state_file, e)
            return
        self.access_token, self.expires_at = access_token, expires_at
        for name, value, domain, path in cookies:
            self.http.cookies.set(name, value, domain=domain, path=path)

    def _save(self) -> None:
        cookies = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
                   for c in self.http.cookies.jar]
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(
            {"access_token": self.access_token, "expires_at": self.expires_at, "cookies": cookies}
        ))
