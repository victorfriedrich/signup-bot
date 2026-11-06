"""The bits of the event site's GraphQL API this bot needs."""

from dataclasses import dataclass
from datetime import datetime

import httpx

EVENT_FIELDS = """
  id title start registrationStart participantLimit participantRegistrationCount
  participantRegistrationPossible userIsRegistered prices
"""


class GraphQLError(Exception):
    pass


@dataclass
class Event:
    id: str
    title: str
    start: datetime
    registration_start: datetime
    limit: int
    registered: int
    possible: bool
    reason: str
    user_is_registered: bool
    prices: dict | None

    @classmethod
    def parse(cls, d: dict) -> "Event":
        possible = d.get("participantRegistrationPossible") or {}
        try:
            return cls(
                id=d["id"], title=d["title"],
                start=datetime.fromisoformat(d["start"]),
                registration_start=datetime.fromisoformat(d["registrationStart"]),
                limit=int(d["participantLimit"]), registered=int(d["participantRegistrationCount"]),
                possible=bool(possible.get("option")), reason=possible.get("reason") or "",
                user_is_registered=bool(d.get("userIsRegistered")), prices=d.get("prices"),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise GraphQLError(f"unexpected event data for {d.get('id')}: {e!r}") from None

    @property
    def free_spots(self) -> int:
        return self.limit - self.registered

    def __str__(self) -> str:
        return f"{self.title} on {self.start:%a %d %b} ({self.registered}/{self.limit})"


class EventApi:
    def __init__(self, http: httpx.AsyncClient, site_url: str):
        self.http = http
        self.graphql = f"{site_url}/graphql"
        # The API rejects requests without the app's origin.
        self.headers = {"origin": site_url, "x-graphql-client-name": "leagcy-app"}

    async def query(self, query: str, variables: dict, token: str | None = None) -> dict:
        headers = {**self.headers, "authorization": f"Bearer {token}"} if token else self.headers
        resp = await self.http.post(self.graphql, json={"query": query, "variables": variables}, headers=headers)
        try:
            body = resp.json()
        except ValueError:
            # An HTML error page from a proxy in front of the API.
            resp.raise_for_status()
            raise GraphQLError(f"non-JSON response (HTTP {resp.status_code})") from None
        if body.get("errors"):
            raise GraphQLError("; ".join(e.get("message", "?") for e in body["errors"]))
        resp.raise_for_status()
        return body["data"]

    async def event(self, event_id: str, token: str | None = None) -> Event:
        data = await self.query(f"query($id: ID!) {{ event(id: $id) {{ {EVENT_FIELDS} }} }}", {"id": event_id}, token)
        return Event.parse(data["event"])

    async def series(self, title: str, after: datetime, token: str | None = None) -> list[Event]:
        """Upcoming events with exactly this title, soonest first."""
        data = await self.query(
            f"query($after: DateTime, $search: String) {{"
            f" events(after: $after, search: $search, limit: 50) {{ {EVENT_FIELDS} }} }}",
            {"after": after.isoformat(), "search": title}, token,
        )
        events = [Event.parse(e) for e in data["events"] if e["title"] == title]
        return sorted(events, key=lambda e: e.start)

    async def register(self, event: Event, token: str) -> bool:
        """Registers as a participant. True once the API says we are registered."""
        variables: dict = {"eventId": event.id}
        if event.prices:
            # Paid events take the default price; the payment itself still has to be done in the app.
            options = event.prices.get("options", [])
            variables["price"] = next((o for o in options if o.get("defaultPrice")), options[0] if options else None)
        data = await self.query(
            """mutation($eventId: ID!, $price: JSON) {
              registerForEvent(eventId: $eventId, registrationType: PARTICIPANT, price: $price) {
                id userIsRegistered participantRegistrationCount
              }
            }""",
            variables, token,
        )
        return bool(data["registerForEvent"]["userIsRegistered"])
