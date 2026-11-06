"""Gets a spot at an event on the site, two ways:

watch:  every ~5 minutes during the day, checks the event for a free spot and
        registers the moment one appears (someone cancelled).
launch: for each launch event, finds the next event in the same series (same
        title) and registers the instant its registration opens, usually
        18:00 three days before.

Both run in one process by default; a launch that loses the race hands its
event to the watcher.
"""

import asyncio
import logging
import random
from datetime import UTC, datetime, timedelta

import httpx

from .api import Event, EventApi, GraphQLError
from .auth import Auth, AuthError
from .config import Config

log = logging.getLogger("bot")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36"
)
ERRORS = (AuthError, GraphQLError, httpx.HTTPError)
# Checks run every few minutes, so one blip is not worth a push; three in a row is.
CHECK_FAILURES_BEFORE_ALERT = 3


def now() -> datetime:
    return datetime.now(UTC)


async def sleep_until(when: datetime) -> None:
    # Sleep in chunks so a long wait is not thrown off by clock adjustments.
    while (left := (when - now()).total_seconds()) > 0:
        await asyncio.sleep(min(left, 300))


class Bot:
    def __init__(self, cfg: Config, auth: Auth, api: EventApi):
        self.cfg = cfg
        self.auth = auth
        self.api = api
        self.watching: set[str] = set(cfg.watch_events) if "watch" in cfg.modes else set()
        self.notify_http = httpx.AsyncClient(timeout=10)
        self.failures: dict[str, int] = {}
        self.launch_titles: set[str] = set()

    async def notify(self, message: str, tags: str = "") -> None:
        if self.cfg.dry_run:
            message = f"[dry run] {message}"
        log.info(message)
        if self.cfg.ntfy_url:
            try:
                await self.notify_http.post(self.cfg.ntfy_url, content=message.encode(), headers={"Tags": tags})
            except httpx.HTTPError as e:
                log.warning("ntfy failed: %s", e)

    async def failed(self, what: str, error: Exception, alert_after: int = 1) -> None:
        """Counts a failure of `what` and pushes an alert once it has failed `alert_after` times in a row."""
        count = self.failures[what] = self.failures.get(what, 0) + 1
        log.warning("%s failed (%d in a row): %s", what, count, error)
        if count == alert_after:
            await self.notify(f"{what} failing: {error}", tags="warning")

    async def recovered(self, what: str, alert_after: int = 1) -> None:
        if self.failures.pop(what, 0) >= alert_after:
            await self.notify(f"{what} working again", tags="white_check_mark")

    async def token(self, min_valid_s: float = 600) -> str:
        try:
            token = await self.auth.token(min_valid_s)
        except ERRORS as e:
            await self.failed("Login", e)
            raise
        await self.recovered("Login")
        return token

    async def register(self, event: Event) -> bool:
        if self.cfg.dry_run:
            log.info("DRY_RUN: would register for %s", event)
            return True
        return await self.api.register(event, await self.token())

    # --- watch ----------------------------------------------------------

    def in_watch_hours(self) -> bool:
        start, end = self.cfg.watch_hours
        hour = now().astimezone(self.cfg.tz).hour
        # A window like 9-2 runs past midnight.
        return start <= hour < end if start < end else hour >= start or hour < end

    async def check(self, event_id: str) -> None:
        # Polling is anonymous so the account only shows up when there is a spot to take.
        event = await self.api.event(event_id)
        if event.start <= now():
            log.info("%s has started; stopped watching it", event)
            self.watching.discard(event_id)
            return
        # Too close to the start to count on seeing the push and getting there; no-shows can be penalised.
        if event.start - now() <= timedelta(hours=self.cfg.watch_stop_hours):
            await self.notify(f"Stopped waiting for a spot at {event}: it starts within "
                              f"{self.cfg.watch_stop_hours:g}h")
            self.watching.discard(event_id)
            return
        if event.free_spots <= 0 or event.registration_start > now():
            log.info("%s: no spot", event)
            return
        event = await self.api.event(event_id, await self.token())
        if event.user_is_registered:
            await self.notify(f"Already registered for {event}; stopped watching it")
        elif event.possible:
            log.info("spot open at %s, registering", event)
            if not await self.register(event):
                return
            await self.notify(f"Registered for {event.title} on {event.start.astimezone(self.cfg.tz):%a %d %b %H:%M}",
                              tags="tada")
        else:
            log.info("%s: %s", event, event.reason or "not open")
            return
        self.watching.discard(event_id)

    async def drop_registered(self) -> None:
        # A restart forgets past signups, and the anonymous checks can't tell a spot is ours,
        # so without this a full event we already hold a spot in would be polled until it starts.
        for event_id in list(self.watching):
            try:
                event = await self.api.event(event_id, await self.token())
            except ERRORS as e:
                log.warning("could not check registration for %s: %s", event_id, e)
                continue
            if event.user_is_registered:
                log.info("already registered for %s; not watching it", event)
                self.watching.discard(event_id)

    async def watch(self) -> None:
        await self.drop_registered()
        while True:
            if self.watching and self.in_watch_hours():
                for event_id in list(self.watching):
                    try:
                        await self.check(event_id)
                    except AuthError:
                        pass  # self.token() already alerted
                    except ERRORS as e:
                        await self.failed("Watch checks", e, CHECK_FAILURES_BEFORE_ALERT)
                    else:
                        await self.recovered("Watch checks", CHECK_FAILURES_BEFORE_ALERT)
            elif not self.watching and "launch" not in self.cfg.modes:
                log.info("nothing left to watch")
                return
            interval = self.cfg.watch_interval_s
            await asyncio.sleep(interval + random.uniform(-0.15, 0.15) * interval)

    # --- launch ---------------------------------------------------------

    async def next_launch(self, title: str) -> Event | None:
        events = await self.api.series(title, now())
        upcoming = [e for e in events if e.registration_start > now()]
        return min(upcoming, key=lambda e: e.registration_start, default=None)

    async def race(self, event: Event) -> tuple[bool, str]:
        """Fires registrations from just before the opening time until one lands. Returns (won, last error)."""
        opens = event.registration_start
        deadline = opens + timedelta(seconds=self.cfg.launch_window_s)
        attempt, last_error = 0, ""
        await sleep_until(opens - timedelta(milliseconds=self.cfg.launch_lead_ms))
        while now() < deadline:
            attempt += 1
            try:
                if await self.register(event):
                    late = (now() - opens).total_seconds()
                    await self.notify(f"Registered for {event.title} on {event.start.astimezone(self.cfg.tz):%a %d %b}"
                                      f" ({late:+.2f}s after opening, attempt {attempt})", tags="tada")
                    return True, ""
            except (GraphQLError, httpx.HTTPError) as e:
                last_error = str(e)
                if attempt == 1 or attempt % 10 == 0:
                    log.info("attempt %d (%+.2fs): %s", attempt, (now() - opens).total_seconds(), e)
                if attempt % 20 == 0 and now() > opens:
                    try:
                        latest = await self.api.event(event.id, await self.token())
                    except ERRORS as check_error:
                        log.info("registration check failed: %s", check_error)
                    else:
                        if latest.user_is_registered:
                            return True, ""
                        if latest.free_spots <= 0:
                            return False, f"filled up ({latest.registered}/{latest.limit})"
            await asyncio.sleep(self.cfg.launch_retry_ms / 1000)
        return False, last_error or "no attempt landed"

    async def launch(self, series_event_id: str) -> None:
        title = self.cfg.series_title if series_event_id == self.cfg.event_id else ""
        if title:
            self.launch_titles.add(title)
        while True:
            try:
                if not title:
                    title = (await self.api.event(series_event_id)).title
                    if title in self.launch_titles:
                        log.info("%r is already followed by another launch event", title)
                        return
                    self.launch_titles.add(title)
                    log.info("launch mode for %r", title)
                event = await self.next_launch(title)
            except ERRORS as e:
                await self.failed("Finding the next event", e, CHECK_FAILURES_BEFORE_ALERT)
                await asyncio.sleep(300)
                continue
            await self.recovered("Finding the next event", CHECK_FAILURES_BEFORE_ALERT)
            if not event:
                log.info("no upcoming %r with registration still to open; looking again in 24h", title)
                await asyncio.sleep(24 * 3600)
                continue
            opens = event.registration_start
            log.info("next: %s, registration opens %s", event, opens.astimezone(self.cfg.tz).strftime("%a %d %b %H:%M:%S"))
            if opens - now() > timedelta(hours=3):
                # No requests until a few hours before, then look again in case the event was moved.
                await sleep_until(opens - timedelta(hours=3))
                continue
            try:
                # Log in hours ahead, with a token that outlasts the race, so a blocked login is
                # pushed while there is still time to switch PROXY_MODE. Afterwards this is cached.
                await self.token(min_valid_s=(opens - now()).total_seconds() + 3600)
            except ERRORS:
                await asyncio.sleep(600)
                continue
            if opens - now() > timedelta(minutes=10):
                await sleep_until(opens - timedelta(minutes=5))
                continue
            won, reason = await self.fire(event)
            if won:
                continue
            await self.notify(f"Missed the launch of {event}: {reason}", tags="x")
            if "watch" in self.cfg.modes:
                log.info("handing %s to the watcher", event.id)
                self.watching.add(event.id)

    async def fire(self, event: Event) -> tuple[bool, str]:
        """Warms up the connection just before `event` opens, then races. Returns (won, reason)."""
        try:
            # A warm connection, so the first attempt is a single round trip.
            # Node closes idle connections after 5s, so the warm-up comes just before the race.
            await sleep_until(event.registration_start - timedelta(milliseconds=self.cfg.launch_lead_ms + 2000))
            await self.api.event(event.id)
            return await self.race(event)
        except ERRORS as e:
            return False, str(e)

    async def test_launch(self, event_id: str, in_s: float) -> None:
        """Runs a launch against a real event as if its registration opened `in_s` seconds from now.
        If the event is really open, this registers for it."""
        try:
            event = await self.api.event(event_id)
            event.registration_start = now() + timedelta(seconds=in_s)
            await self.notify(f"TEST launch: {event} 'opens' at "
                              f"{event.registration_start.astimezone(self.cfg.tz):%H:%M:%S}", tags="test_tube")
            await self.token(min_valid_s=in_s + 3600)
        except ERRORS as e:
            await self.notify(f"TEST launch could not start: {e}", tags="x")
            return
        won, reason = await self.fire(event)
        if not won:
            await self.notify(f"TEST launch missed {event}: {reason}", tags="x")


async def run(cfg: Config) -> None:
    proxy = cfg.proxy
    auth_http = httpx.AsyncClient(proxy=proxy, timeout=30, headers={"user-agent": USER_AGENT})
    api_http = httpx.AsyncClient(
        proxy=proxy if cfg.proxy_mode == "all" else None, timeout=15,
        headers={"user-agent": USER_AGENT}, limits=httpx.Limits(keepalive_expiry=60),
    )
    auth = Auth(auth_http, cfg.auth_url, cfg.auth0_client_id, cfg.auth0_audience, cfg.site_url,
                cfg.email, cfg.password, cfg.session_cookie, cfg.state_dir)
    bot = Bot(cfg, auth, EventApi(api_http, cfg.site_url))
    log.info("modes: %s%s; proxy: %s", ",".join(sorted(cfg.modes)), " (dry run)" if cfg.dry_run else "",
             cfg.proxy_mode)
    # One login at startup so bad credentials are pushed now, not at 18:00. A saved token is reused.
    # A failure doesn't stop the bot: polling needs no login, and later logins retry.
    try:
        await bot.token()
    except ERRORS:
        pass
    tasks = []
    if "watch" in cfg.modes:
        tasks.append(bot.watch())
    if "launch" in cfg.modes:
        tasks.extend(bot.launch(e) for e in cfg.launch_events)
    if cfg.test_launch_event:
        tasks.append(bot.test_launch(cfg.test_launch_event, cfg.test_launch_in_s))
    try:
        await asyncio.gather(*tasks)
    except Exception as e:
        await bot.notify(f"Bot crashed, Railway will restart it: {e!r}", tags="rotating_light")
        raise


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run(Config()))


if __name__ == "__main__":
    main()
