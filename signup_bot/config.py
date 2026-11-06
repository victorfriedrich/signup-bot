import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

UUID =re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def event_id(value: str) -> str:
    """Accepts a bare event id or an event URL."""
    match = UUID.search(value.lower())
    if not match:
        raise ValueError(f"no event id in {value!r}")
    return match.group(0)


def proxy_url() -> str | None:
    if url := env("DECODO_PROXY_URL"):
        return url
    user, password = env("DECODO_USERNAME"), env("DECODO_PASSWORD")
    if not (user and password):
        return None
    return f"http://{quote(user, safe='')}:{quote(password, safe='')}@{env('DECODO_ENDPOINT', 'gate.decodo.com:10001')}"


@dataclass
class Config:
    # The event site and its Auth0 login; see "Site settings" in the README.
    site_url: str = field(default_factory=lambda: env("SITE_URL").rstrip("/"))
    auth_url: str = field(default_factory=lambda: env("AUTH_URL").rstrip("/"))
    auth0_client_id: str = field(default_factory=lambda: env("AUTH0_CLIENT_ID"))
    auth0_audience: str = field(default_factory=lambda: env("AUTH0_AUDIENCE"))

    email: str = field(default_factory=lambda: env("LOGIN_EMAIL"))
    password: str = field(default_factory=lambda: env("LOGIN_PASSWORD"))
    session_cookie: str = field(default_factory=lambda: env("AUTH0_SESSION_COOKIE"))

    modes: set[str] = field(default_factory=lambda: {
        m.strip() for m in env("MODE", "watch,launch").lower().replace("both", "watch,launch").split(",")
    })
    event_id: str = field(default_factory=lambda: event_id(env("EVENT")) if env("EVENT") else "")
    watch_events: list[str] = field(default_factory=lambda: [
        event_id(e) for e in env("WATCH_EVENTS").split(",") if e.strip()
    ])
    # Launch mode follows the series (title) of each of these.
    launch_events: list[str] = field(default_factory=lambda: [
        event_id(e) for e in env("LAUNCH_EVENTS").split(",") if e.strip()
    ])
    # Test switch: race this event as if its registration opened TEST_LAUNCH_IN_S after startup.
    test_launch_event: str = field(default_factory=lambda: event_id(env("TEST_LAUNCH_EVENT")) if env("TEST_LAUNCH_EVENT") else "")
    test_launch_in_s: float = field(default_factory=lambda: float(env("TEST_LAUNCH_IN_S", "120")))
    # Defaults to the title of EVENT, which is how the weekly copies of one class are told apart.
    series_title: str = field(default_factory=lambda: env("SERIES_TITLE"))

    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo(env("TIMEZONE", "Europe/Berlin")))
    watch_interval_s: float = field(default_factory=lambda: float(env("WATCH_INTERVAL_MIN", "5")) * 60)
    watch_hours: tuple[int, int] = field(default_factory=lambda: tuple(
        int(h) for h in env("WATCH_HOURS", "9-2").split("-")
    ))
    watch_stop_hours: float = field(default_factory=lambda: float(env("WATCH_STOP_HOURS", "3")))

    launch_lead_ms: int = field(default_factory=lambda: int(env("LAUNCH_LEAD_MS", "1000")))
    launch_retry_ms: int = field(default_factory=lambda: int(env("LAUNCH_RETRY_MS", "150")))
    launch_window_s: int = field(default_factory=lambda: int(env("LAUNCH_WINDOW_S", "20")))

    # off: no proxy, even with Decodo credentials set. auth: only the Auth0 login goes through it.
    # all: the GraphQL calls too.
    proxy_mode: str = field(default_factory=lambda: env("PROXY_MODE", "off").lower())
    proxy: str | None = field(default_factory=proxy_url)

    state_dir: Path = field(default_factory=lambda: Path(env("STATE_DIR", "state")))
    dry_run: bool = field(default_factory=lambda: env("DRY_RUN").lower() in ("1", "true", "yes"))
    ntfy_url: str = field(default_factory=lambda: env("NTFY_URL"))

    def __post_init__(self) -> None:
        unknown = self.modes - {"watch", "launch"}
        if unknown or not self.modes:
            raise ValueError(f"MODE must be watch, launch or both, got {sorted(unknown)}")
        missing = [name for name, value in (
            ("SITE_URL", self.site_url), ("AUTH_URL", self.auth_url),
            ("AUTH0_CLIENT_ID", self.auth0_client_id), ("AUTH0_AUDIENCE", self.auth0_audience),
        ) if not value]
        if missing:
            raise ValueError(f"set {', '.join(missing)}")
        if not self.watch_events and self.event_id:
            self.watch_events = [self.event_id]
        if not self.launch_events and self.event_id:
            self.launch_events = [self.event_id]
        if not ((self.email and self.password) or self.session_cookie):
            raise ValueError("set LOGIN_EMAIL and LOGIN_PASSWORD, or AUTH0_SESSION_COOKIE")
        if self.proxy_mode not in ("off", "auth", "all"):
            raise ValueError("PROXY_MODE must be off, auth or all")
        if self.proxy_mode == "off":
            self.proxy = None
        elif not self.proxy:
            raise ValueError(f"PROXY_MODE={self.proxy_mode} needs DECODO_USERNAME and DECODO_PASSWORD")
