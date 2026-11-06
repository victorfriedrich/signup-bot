# signup-bot

> **Work in progress.** This is an experimental personal project, not a finished tool. It talks to the event site's private API, which can change without notice, and it has only been tested with one account. Expect rough edges, and check the logs after every change.

Gets you a spot at events on a sign-up site where places run out fast. It does two jobs:

- **Waiting list**: The site has no real waiting list, so the bot checks a full event every ~5 minutes and registers you the moment someone cancels.
- **Sign-up at opening time**: popular classes fill up within seconds of registration opening. The bot registers you at that exact moment.

It runs as one always-on worker on [Railway](https://railway.com).

## How it's mostly used: through an agent

In practice nobody edits config by hand. You ask a coding agent (Claude Code, for example) in this folder, while you're signed in to the event site in Chrome:

> Add me to the waiting list for the Ice Skating event, use Chrome to get its ID.

> Sign me up for the Bachata class the moment it opens, use Chrome to get its ID.

> Take the BMW Factory Tour off the waiting list.

The agent then:

1. Finds the event on the site in your Chrome and reads its ID from the URL.
2. Checks the event (title, date, spots, when registration opens) and tells you what it found. Dance classes come as two events, Leader and Follower, so say which one you want.
3. Adds the ID to `WATCH_EVENTS` (waiting list) or `LAUNCH_EVENTS` (sign-up at opening time) on Railway. `railway variables --set` replaces the whole list, so the agent first reads the current value and keeps the events already on it.
4. Redeploys (the bot only reads its settings at startup) and checks the startup log, which lists every event and when it will act.

The agent never needs your password. That lives in the Railway variables (see below).

The agent can also set the bot up from scratch ("deploy this to Railway") by following [Deploying to Railway](#deploying-to-railway) step by step. You sign in to Railway and type your password; it does the rest. It also redeploys after code changes. Claude Code's auto mode may ask you to approve those deploys.

## Prerequisites

- **An account on the event site that can already register for events.** Sign up once on the site, finish your profile, and make sure you can register for an event by hand. The bot can't do anything your account isn't allowed to do.
- **Email + password login** for that account, **without two-factor authentication**: the bot can't get past a 2FA prompt. If you sign in with Google, Microsoft or Facebook instead, there's no password to give the bot; see "Where your details go" for the cookie workaround, which is untested.
- **A Railway account** and the [Railway CLI](https://docs.railway.com/guides/cli) (`railway login`) to run it around the clock. An always-on service isn't free on Railway beyond the trial credit; it fits in the Hobby plan. Running it on your laptop works too, but only while the laptop is awake.
- **Python 3.12** if you want to try it locally.
- **The [ntfy](https://ntfy.sh) app** on your phone. Optional, but without it you only find out what happened from the logs, including a missed launch or a failed login. See [Notifications](#notifications).
- Optional, and only if logins start getting blocked: a [Decodo](https://decodo.com) residential proxy. See [Proxy](#proxy-decodo).

## Where your details go

Your login never goes in the code or in git.

- **On Railway** (how it actually runs): set the variables on the service, in the dashboard under *Variables* or with `railway variable set NAME=value`. Set at least the four site settings below, `LOGIN_EMAIL` and `LOGIN_PASSWORD`.
- **Locally**: copy `.env.example` to `.env` and fill it in. `.env` is gitignored.

**Untested:** if you sign in with Google, Microsoft or Facebook, leave the password empty and copy the `auth0` cookie of the login domain (`AUTH_URL`) from your browser's developer tools into `AUTH0_SESSION_COOKIE`. The bot keeps that session alive, but once Auth0's absolute limit runs out (usually 30 days) you have to copy a fresh cookie.

The bot saves its login token in `STATE_DIR` (`/data` on Railway, `state/` locally, also gitignored), so it doesn't log in again on every restart.

## Site settings

The code doesn't name the site it talks to. Four settings tell it:

| Variable | What it is | Where to find it |
|---|---|---|
| `SITE_URL` | The event site, e.g. `https://<site>` | Your browser's address bar |
| `AUTH_URL` | Its Auth0 login domain | Click *Log in* on the site; the page you land on is at `https://<login domain>/authorize?…` |
| `AUTH0_CLIENT_ID` | The web app's public Auth0 client ID | The `client_id=` parameter of that `/authorize` URL |
| `AUTH0_AUDIENCE` | The API the token is for | The `audience=` parameter of the same URL |

None of these is a secret; the site sends them to every visitor.

## Finding an event's ID

Every event page has its ID in the URL:

```
https://<site>/events/01234567-89ab-cdef-0123-456789abcdef
                      └──────────── event ID ────────────┘
```

Open the event and copy the URL. The bot accepts either the full URL or just the ID.

You can also search from the terminal, no login needed:

```bash
curl -s "$SITE_URL/graphql" -H 'content-type: application/json' -H "origin: $SITE_URL" -H 'x-graphql-client-name: leagcy-app' -d '{"query":"query($search: String){ events(search: $search, limit: 20){ id title start registrationStart participantLimit participantRegistrationCount } }","variables":{"search":"bachata"}}'
```

## Using it by hand

The two lists, as Railway variables:

| Variable | What it does |
|---|---|
| `WATCH_EVENTS` | Waiting list. Comma-separated event IDs or URLs. Each is checked every ~5 minutes from 09:00 to 02:00 Berlin time until you hold a spot, or until 3 hours before it starts. |
| `LAUNCH_EVENTS` | Sign-up at opening time. Comma-separated event IDs or URLs. For each, the bot follows the **series**, i.e. every upcoming event with exactly the same title, and registers you for the next one the moment its registration opens. A weekly class keeps getting booked until you remove it. |

For example, to wait for a cancellation at the Ice Skating event and the BMW Factory Tour, and grab the Bachata Class every week:

```bash
railway variable set "WATCH_EVENTS=<Ice Skating id>,<BMW Factory Tour id>" --skip-deploys
railway variable set "LAUNCH_EVENTS=<id of any week's Bachata Class>"
```

Setting a variable without `--skip-deploys` redeploys the service. Then check the startup log:

```bash
railway logs
```

```
bot next: Bachata Class on Tue 17 Nov (0/30), registration opens Sat 14 Nov 18:00:00
bot Ice Skating on Wed 11 Nov (25/25): no spot
bot BMW Factory Tour on Thu 12 Nov (23/23): no spot
```

If a launch loses the race, the bot moves that event onto the waiting list.

Free events only. For a paid event the bot picks the default price, but you still have to pay in the app. And keep in mind that not showing up to an event you got through the waiting list can get you barred from future ones.

## Trying it locally

Without registering for anything:

```bash
cp .env.example .env   # then fill in your login and events
python -m venv .venv && .venv/bin/pip install -r requirements.txt
set -a; . ./.env; set +a; DRY_RUN=true STATE_DIR=state .venv/bin/python -m signup_bot
```

## Deploying to Railway

Railway builds the `Dockerfile` from this folder; nothing is connected to GitHub. Run these from the repo folder, in this order. The order matters: the bot exits at startup without a login, so the variables and the volume have to exist before the first deploy.

1. Sign in. You do this yourself, since it opens the browser:
   ```bash
   railway login
   ```
2. Create the project and an empty service, and link this folder to it:
   ```bash
   railway init --name signup-bot
   railway add --service signup-bot
   railway service link signup-bot
   ```
3. Add the volume that keeps the login across restarts:
   ```bash
   railway volume add --service signup-bot --mount-path /data
   ```
4. Set the site, your login and your events (see [Site settings](#site-settings)). Type the password yourself; it isn't echoed and doesn't end up in your shell history:
   ```bash
   railway variable set SITE_URL=https://<site> --skip-deploys
   railway variable set AUTH_URL=https://<login domain> --skip-deploys
   railway variable set AUTH0_CLIENT_ID=<client id> --skip-deploys
   railway variable set AUTH0_AUDIENCE=<audience> --skip-deploys
   railway variable set LOGIN_EMAIL=you@example.com --skip-deploys
   read -s "p?Site password: "; printf %s "$p" | railway variable set LOGIN_PASSWORD --stdin --skip-deploys; unset p
   railway variable set "WATCH_EVENTS=<event id>,<event id>" --skip-deploys
   railway variable set "LAUNCH_EVENTS=<event id>" --skip-deploys
   railway variable set NTFY_URL=https://ntfy.sh/<your long random topic> --skip-deploys
   ```
   Outside Germany, also set `TIMEZONE` (e.g. `Europe/Vienna`): the watch hours and the times in pushes use it.
   (`read -s "p?…"` is zsh; in bash use `read -sp "Site password: " p`.)
5. Deploy:
   ```bash
   railway up --detach
   ```
6. Check it. The log should show the login and one line per event:
   ```bash
   railway logs
   ```
   Then open the service's settings in the dashboard (`railway open`) and check *Regions*: exactly **one replica, in EU West**. `railway.json` asks for that, but the first deploy of this bot still landed in the US. Never run two replicas: two bots would both race at opening time and both poll. To remove a region, scale it to zero by the id the dashboard shows (`railway scale <region-id>=0`); the short names like `us-west` didn't match last time.

After that, every code change is just `railway up --detach`, and every list change is `railway variable set …` (which redeploys on its own). `railway variable delete` doesn't redeploy; follow it with `railway up --detach`.

To pause the bot, run `railway down` (removes the running deployment; your variables and volume stay). `railway up --detach` starts it again.

`railway.json` uses Railway's config-as-code format, which stops working on 2026-12-01. Run `railway config migrate` before then.

### Notes for agents

- The agent can run every step except `railway login` and typing the password.
- `railway variable set NAME=value` replaces the whole value. Read the list first (`railway variable list --kv`) and keep what's there.
- Claude Code's auto mode may block `railway up` and `railway variable set` as production deploys. Ask the user to approve, then run the same command again.
- A deploy is done when `railway logs` shows the `next: …` and `… no spot` lines for every event. A `Bot crashed` or `Login failing` line means it isn't.

## Notifications

ntfy needs no account. Make up a long random topic name (anyone who guesses it can read your pushes), subscribe to it in the ntfy app, and set `NTFY_URL=https://ntfy.sh/<that topic>`. The bot then pushes:
- a registration, from either list
- a missed launch, with the reason
- giving up on a waiting-list event 3 hours before it starts
- a failed login, at once. A Cloudflare block on the login domain is named as such, with a hint to set `PROXY_MODE=auth`.
- checks or the next-event lookup failing 3 times in a row, and a "working again" notice once they recover
- a crash, just before Railway restarts the bot

## How it talks to the site

There's no browser involved. The bot logs in to Auth0 (`AUTH_URL`) over plain HTTP, using the same code+PKCE flow as the web app. It then calls `registerForEvent` on the site's GraphQL API (`SITE_URL/graphql`) with the resulting token. It keeps the Auth0 session cookie, so later tokens come from a silent `prompt=none` refresh rather than a fresh password login.

Checking events and finding the next one in a series are anonymous. Your account only makes requests in three cases:
- at startup: one login to catch bad credentials early (skipped while a saved token is still valid), and a check of each waiting-list event, so one you already hold a spot in isn't polled again
- 3 hours before a launch, a token check that logs in only if the saved token won't outlast the race, followed at opening time by the registration attempts (from 1 s early, every 150 ms, for 20 s)
- when the waiting list sees a free spot

With an empty waiting list the bot makes no regular requests: launch mode looks up the next event at startup, then again 3 hours and 5 minutes before it opens (once a day if no event is listed yet).

## Proxy (Decodo)

You probably don't need this. The bot runs without a proxy by default. It exists for one case: the login domain sits behind Cloudflare, which may start blocking logins from Railway's datacenter IPs. If that happens, the bot pushes a "Cloudflare answered HTTP 403" alert, and a residential proxy for the login is the fix. Checking events and the race itself never need it.

Set `DECODO_PROXY_URL` (`http://user:pass@gate.decodo.com:10001`), or set `DECODO_USERNAME` + `DECODO_PASSWORD` and optionally `DECODO_ENDPOINT`. Use a sticky port (10001+), not the rotating one, so the steps of one login share an IP.

`PROXY_MODE` switches the proxy on and off:
- `off` (the default): the Decodo credentials can stay set but aren't used.
- `auth`: only the Auth0 login goes through the proxy. Auth0 sits behind Cloudflare, so a datacenter IP is where blocks would come from. Turn this on if logins from Railway start failing.
- `all`: the API calls go through it too. The site's API is a plain Express server, though, and a residential hop would slow down the 18:00 race.

## All settings

| Variable | Default | |
|---|---|---|
| `SITE_URL` / `AUTH_URL` / `AUTH0_CLIENT_ID` / `AUTH0_AUDIENCE` | | Required; see [Site settings](#site-settings) |
| `LOGIN_EMAIL` / `LOGIN_PASSWORD` | | Your login on the event site |
| `AUTH0_SESSION_COOKIE` | | Instead of a password, for Google/Microsoft/Facebook accounts (untested) |
| `WATCH_EVENTS` | `EVENT` | Waiting list, comma-separated |
| `LAUNCH_EVENTS` | `EVENT` | Series to sign up for at opening time, comma-separated |
| `EVENT` | | Fallback for both lists when they're empty |
| `SERIES_TITLE` | title of `EVENT` | Exact title launch mode looks for in `EVENT`'s series |
| `MODE` | `both` | `watch`, `launch` or `both` |
| `TIMEZONE` | `Europe/Berlin` | For the watch hours and the times in logs and pushes |
| `WATCH_INTERVAL_MIN` / `WATCH_HOURS` | `5` / `9-2` | ±15% jitter on the interval; hours may wrap past midnight |
| `WATCH_STOP_HOURS` | `3` | Stop waiting for a cancellation this long before the event starts, with a push |
| `LAUNCH_LEAD_MS` / `LAUNCH_RETRY_MS` / `LAUNCH_WINDOW_S` | `1000` / `150` / `20` | |
| `NTFY_URL` | | Push notifications |
| `PROXY_MODE` | `off` | `off`, `auth` or `all`; see [Proxy](#proxy-decodo) |
| `DECODO_PROXY_URL`, or `DECODO_USERNAME` / `DECODO_PASSWORD` / `DECODO_ENDPOINT` | | The proxy, used only when `PROXY_MODE` isn't `off` |
| `STATE_DIR` | `/data` in the Docker image | Where the login token is saved |
| `DRY_RUN` | `false` | Everything except the actual registration; pushes are marked `[dry run]` |
| `TEST_LAUNCH_EVENT` / `TEST_LAUNCH_IN_S` | / `120` | Test switch: race this event as if its registration opened this many seconds after startup. If it's really open, this registers you. Remove it afterwards, or every restart races again |
