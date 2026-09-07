# botc-discord-bot

A Discord bot with slash commands that look up a Blood on the Clocktower script on a
[botc-scripts](https://github.com/DomBennett/botc-scripts) instance:

* **`/script`** — the script's PDF **rendered to images**, posted inline so everyone can
  read the script in the channel without downloading anything. **Public by default.**
* **`/json`** — the script's **JSON** as a downloadable `.json` file, ready to paste into
  the official app or the Clocktower online tools. **Private by default**, and much
  faster: it never downloads or rasterises the PDF, and usually makes a single request.
* **`/alias`** — server administrators only: give a script a **custom id**, so it can be
  looked up as `sects` rather than `13108`. Needs a self-hosted botc-scripts fork — see
  [Custom ids](#custom-ids).

```
/script query:Sects and Violets
/script query:13108
/script query:sects
/script query:No Greater Joy version:3.5.0
/script query:Sects and Violets output:private

/json   query:Sects and Violets
/json   query:13108 output:public

/alias  set   query:13108 custom_id:sects
/alias  show  query:Sects and Violets
/alias  clear query:sects
```

`/script` and `/json` take the same parameters:

| Parameter | Required | Meaning |
| --- | --- | --- |
| `query` | **yes** | A script name (fuzzy-matched), a **custom id**, or a numeric script id. Offers suggestions as you type |
| `version` | no | A version such as `1.0.0`. Defaults to the latest |
| `output` | no | `public` (everyone in the channel sees it) or `private` (only you). Defaults to **public** for `/script` and **private** for `/json` |

`output` is per-invocation: it overrides that command's default for that one run and
changes nothing else.

## Suggestions as you type

Typing into `query` offers up to 25 scripts, and picking one submits that script's
**canonical id** — its custom id where it has one, its numeric id otherwise — so the bot
resolves it exactly rather than re-running the fuzzy name search. A script with a custom
id shows it in the suggestion, as `Sects and Violets (sects) — TPI`, which is the
cheapest way for people to learn that it has one. Suggestions come from two places, in
order:

1. **Scripts this bot has already served**, most recent first, preferring ones served in
   the same server. With no text typed yet, this is simply the recent list.
2. **A live search of the botc-scripts instance** for anything not already suggested.

The live search is best-effort, on a budget well inside Discord's 3-second autocomplete
deadline. If the instance is slow or down, the suggestions quietly fall back to the cached
ones alone. Suggestions are only ever suggestions: typing a name or id that matches
nothing in the list still works exactly as it always did.

## How it behaves at the edges

| Situation | What the bot does |
| --- | --- |
| Name matches nothing | Says so, and offers the closest names it found |
| Name matches several scripts | Lists the top matches **with their ids** and asks you to re-run with one — it never guesses |
| Script exists but has no PDF uploaded | `/script` says there are no pages to show and points you at `/json`, which still works for that script |
| PDF has more pages than the render cap | Posts the first `BOTC_MAX_PAGES` (10 by default) and states how many pages were left out, with a link to the full PDF |
| A page would exceed the upload size limit | Drops to JPEG, then steps the DPI down to 50, and says if pages were still left out |
| The JSON is over the upload limit | `/json` says so and links the script's page rather than failing the upload |
| Upstream is down, slow, or returns an error | Reports the failure at the same visibility the invocation chose — the interaction is never left hanging |
| A custom id is malformed or reads as a number | `/alias` says why before it sends anything, so a typo costs no request |
| A custom id is taken, or reserved by the instance | Shows the instance's own refusal, word for word — it never picks a different id for you |
| `/alias` on the public site, or with no API credentials | Says which of the two it is, and what to set, rather than failing at the API |

## Custom ids

**Custom ids need a self-hosted botc-scripts fork.** The public site addresses a script
only by its numeric id and its API has no field for anything else, so pointed at
<https://www.botcscripts.com> this bot behaves exactly as it always did and `/alias` says
so rather than failing obscurely.

On a fork that has the feature, a script can be given one short string id — `sects` — and
that id is canonical: it works anywhere a numeric id works. In `/script` and `/json`, in
autocomplete, and in the page link the bot posts beside a script.

### What a custom id may look like

| Rule | Why |
| --- | --- |
| Lowercase letters and digits, separated by single hyphens: `sects-and-violets` | One spelling per id, and safe in both a URL and a filename |
| 2–50 characters | Anything shorter is not worth typing instead of a number |
| **Never a number.** `13108`, `007`, `-12` and `1_0` are all refused | A query that reads as a number is looked up as a *script id* first, so a numeric custom id would silently resolve to whichever script holds that id — a wrong answer rather than an error, and only once the instance has grown that far |
| Not a word the site's own URLs use — `search`, `upload`, `api`, `admin` … | The instance reserves them, and refuses them |
| Unique, case-insensitively | `Sects` and `sects` are the same id, not two |

Case is folded rather than refused, so `/alias set custom_id:SECTS` succeeds and stores
`sects`. The bot checks the shape before it sends anything, so a typo costs no request.
Uniqueness and reserved words depend on state the bot does not have, so they are the
instance's to judge — and its refusal is shown to you as it was written.

### `/alias`

| Subcommand | Does |
| --- | --- |
| `/alias set query:<script> custom_id:<id>` | Gives that script the id, replacing any it had |
| `/alias clear query:<script>` | Removes it |
| `/alias show query:<script>` | Says what it is, or how to give it one |

`query` takes everything `/script` takes: a name, a numeric id, or the current custom id.

* **Administrators only.** The command is registered with `default_member_permissions`
  of `0`, so Discord hides it from everyone else — but that is a server-side hint a
  server admin can re-grant to a role, so each subcommand also checks `administrator`
  locally on every invocation. A refusal is answered privately, and is not logged as a
  fault.
* **Always private.** Replies are ephemeral and there is no `output` parameter: they name
  the instance's host, the bot's API user and other scripts' ids, and they are
  configuration rather than channel content.
* **`set` and `clear` need API credentials**; `show` does not, because reading custom ids
  is anonymous. With none configured, `/alias set` says exactly which variables are
  missing and sends nothing.

## Prerequisites

* Python 3.11 or newer.
* A Discord application with a bot user.
* Network access to a botc-scripts instance (the public site by default).

No system packages are needed. The PDF renderer, `pypdfium2`, ships PDFium inside its
wheel, so there is no poppler or MuPDF to install.

## Creating the Discord application

1. Go to <https://discord.com/developers/applications> and click **New Application**.
2. Open **Bot** in the sidebar, then **Reset Token**, and copy the token. This is the value
   of `DISCORD_TOKEN`. Treat it like a password — anyone with it controls the bot.
3. Leave every **Privileged Gateway Intent** switched **off**. This bot requests no gateway
   intents at all; slash commands arrive regardless.
4. Open **Installation** (or **OAuth2 → URL Generator** on older layouts) and build an
   invite URL with:
   * **Scopes:** `bot` and `applications.commands`
   * **Bot permissions:** `Send Messages` and `Attach Files`

   That is the complete set. The bot reads no messages, joins no voice channels, and needs
   nothing else. If you use the URL Generator directly, the resulting link looks like:

   ```
   https://discord.com/api/oauth2/authorize?client_id=YOUR_APP_ID&permissions=34816&scope=bot%20applications.commands
   ```

   (`34816` = Send Messages + Attach Files.)
5. Open that URL and add the bot to your server.

## Install

```bash
git clone <your-fork> botc-discord-bot
cd botc-discord-bot

python3 -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

For the tests and linter as well, use `pip install -r requirements-dev.txt`.

## Configure

```bash
cp .env.example .env
```

Then edit `.env`:

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `DISCORD_TOKEN` | **yes** | — | Bot token from the Developer Portal |
| `BOTC_BASE_URL` | no | `https://www.botcscripts.com` | The botc-scripts instance to query |
| `DISCORD_GUILD_ID` | no | — | Register commands to one server, instantly |
| `BOTC_RENDER_DPI` | no | `150` | Render resolution, 50–400 |
| `BOTC_MAX_PAGES` | no | `10` | Pages to render, 1–10 |
| `BOTC_HTTP_TIMEOUT` | no | `60` | Total seconds per upstream request |
| `BOTC_MAX_PDF_BYTES` | no | `62914560` | Refuse to download PDFs larger than this |
| `BOTC_CACHE_PATH` | no | `botc-suggestions.sqlite3` | Where the autocomplete cache lives |
| `BOTC_CACHE_ENTRIES` | no | `500` | Rows the cache keeps, 0–100000. `0` disables it |
| `BOTC_API_USER` | no | — | **Self-host only.** User for `/alias set` and `/alias clear` |
| `BOTC_API_PASSWORD` | no | — | **Self-host only.** That user's password |
| `LOG_LEVEL` | no | `INFO` | `CRITICAL`, `ERROR`, `WARNING`, `INFO` or `DEBUG` |

`.env` is in `.gitignore`. Never commit it — commit `.env.example` instead.

**The two `BOTC_API_*` settings are only for a self-hosted fork with
[custom ids](#custom-ids).** They must name a Django user on your instance holding the
`scripts.api_write_permission` permission; the bot sends them as HTTP Basic, and only
ever to set or clear a custom id — every other request it makes is anonymous. Leave both
unset and the bot is read-only, which is the default and all the public site supports.

Set **both or neither**: half a credential is a startup error rather than a bot that
silently cannot write. Over a plain `http://` base URL they cross the network
unencrypted, which is fine inside a private network — `http://botc-scripts:8000` on a
Docker Compose bridge, say — and logged as a warning at startup so it is not a surprise
anywhere else.

**Set `DISCORD_GUILD_ID` while developing.** Guild-scoped commands appear the moment the
bot starts. Global commands (what you get with the variable unset) can take up to an hour
to propagate, so use guild scope until you are ready to ship.

## The suggestion cache

Autocomplete is backed by a small SQLite file — `botc-suggestions.sqlite3` next to the bot
by default, or wherever `BOTC_CACHE_PATH` points. It is created on first use, and SQLite
adds `-wal` and `-shm` sidecar files beside it. All three are in `.gitignore`.

**What it stores**, one row per script per server, written only when a script was actually
delivered:

| Column | Why |
| --- | --- |
| `script_id` | Identity, and the value a picked suggestion submits when there is no custom id |
| `name`, `author` | The label shown in the suggestion list |
| `slug` | The script's [custom id](#custom-ids), where it has one: shown in the label, submitted as the value, and matched when you type it. Kept here so suggestions still carry it when the instance is slow — which is exactly when the cache is answering |
| `guild_id` | So a server's own recent scripts are offered first (`0` in DMs) |
| `used_at` | Recency ordering, and which rows get evicted |

A database created before custom ids existed gains the `slug` column automatically on the
next start; nothing needs deleting and no suggestions are lost.

**What it does not store:** the script JSON, the PDF, the rendered pages, anything about
*who* ran a command, and any Discord CDN URL. Serving a script again refreshes its row
rather than adding another, and the table is capped at `BOTC_CACHE_ENTRIES` rows — the
oldest are deleted past that.

**It is local to this bot.** Nothing is uploaded anywhere, and no second instance reads it.

**To clear it**, stop the bot and delete the file and its sidecars:

```bash
rm -f botc-suggestions.sqlite3 botc-suggestions.sqlite3-wal botc-suggestions.sqlite3-shm
```

The bot recreates it empty on the next lookup and refills it as it serves. Deleting it
costs nothing but the recent-scripts half of autocomplete — the live search still answers.
Set `BOTC_CACHE_ENTRIES=0` to turn the cache off altogether; no file is then created.

## Run

```bash
source .venv/bin/activate
python bot.py
```

The bot syncs its command tree on startup and then waits for interactions. Stop it with
Ctrl-C.

Adding `/alias` changed the registered command payload, so **the tree must re-sync** —
instant with `DISCORD_GUILD_ID` set, up to an hour globally without it. `/alias` appears
only for server administrators, so if you cannot see it, check that first.

## Pointing at a self-hosted botc-scripts

Set `BOTC_BASE_URL` to your instance's origin, with no trailing path:

```bash
BOTC_BASE_URL=https://scripts.example.com
```

The bot only uses endpoints that a stock botc-scripts deployment exposes anonymously:

* `GET /api/scripts/` — fuzzy search, and the script JSON inline in the `content` field
* `GET /api/script_ids/<script_pk>/` — the version list for one script
* `GET /api/scripts/<version_pk>/json/` — the JSON, if `content` was missing
* `GET /script/<script_pk>/<version>/download_pdf` — the uploaded PDF

Two more are used only when the instance has [custom ids](#custom-ids), and both are
optional: an instance without them answers 404, which this bot reads as "no such custom
id" and falls through to the ordinary name search.

* `GET /api/script_ids/slug/<custom_id>/` — look one up, anonymous like the rest
* `PATCH /api/script_ids/<script_pk>/slug/` — set or clear one. The only write this bot
  ever makes, only from `/alias`, and only with `BOTC_API_USER` / `BOTC_API_PASSWORD`

Two things to know about self-hosting:

* **The PDF only comes from `download_pdf`.** The read API exposes no `pdf` field, and a
  default self-hosted deployment serves no media URL at all (`botc/urls.py` does not route
  `MEDIA_URL`). Streaming through the Django view is the only portable way to get the bytes,
  so that is what this bot does — it never scrapes the page or hits a CDN URL.
* **TLS uses your operating system's trust store**, via `truststore`. An instance behind a
  private or corporate CA works as long as the machine running the bot already trusts that
  CA. No extra configuration, and no `verify=False` anywhere.

## Known limits

These come from Discord, not from this bot:

* **Private replies carry files perfectly well.** A `private` reply is an *ephemeral*
  Discord message, and ephemeral messages take attachments like any other — there is no
  fallback and no DM workaround. Two things do differ: Discord removes ephemeral
  attachments after a while, and only you can see the message, so save the `.json` if you
  want to keep it.
* **Visibility is fixed when the command starts.** A reply's public/private state cannot be
  changed once it exists, so `output` is read before any work begins and every message of
  that invocation — including errors and any second page of images — matches it.
* **10 attachments per message.** `/script` renders at most 10 pages and `/json` sends one
  file, so in practice everything arrives in one message; the bot still splits into a
  follow-up if it ever needs to, and keeps that follow-up at the chosen visibility.
* **Page cap.** At most 10 pages are ever rendered (`BOTC_MAX_PAGES`). Longer PDFs are
  truncated, and the bot always says how many pages it left out and links the full PDF.
* **Upload size.** The per-file limit is read live from the interaction
  (`Interaction.filesize_limit`), which already accounts for the server's boost tier and the
  caller's Nitro status — no hardcoded guess. The bot keeps 5% headroom, caps a whole
  message at 25 MiB, and if a page still will not fit it switches PNG → JPEG (quality 85,
  70, 55) and then steps the DPI down to a floor of 50.
* **3-second response deadline / 15-minute follow-up window.** Both commands defer
  immediately, which converts the 3-second deadline into a 15-minute budget for the
  download, render and upload.
* **Autocomplete gets 3 seconds and cannot be deferred.** The callback reads the local
  cache and gives the live search a fraction of a second; on any slowness or failure it
  returns what it has, or nothing, rather than making you wait.
* **Rate limit.** `/script` allows two invocations per user per 15 seconds, to keep a
  channel from queueing many multi-megabyte downloads at once. `/json` has its own, laxer
  bucket of six per 15 seconds, because it downloads nothing. `/alias` has a third, four
  per 30 seconds: a couple of small requests, but administrators-only and rarely run in
  bursts. A non-administrator refused by `/alias` spends nothing from its bucket.

## Layout

```
bot.py                    entry point
botcbot/config.py         environment parsing
botcbot/botcscripts.py    botc-scripts API client — no Discord import, unit-testable
botcbot/rendering.py      blocking PDF rasterisation, run via asyncio.to_thread
botcbot/cache.py          blocking SQLite suggestion cache, same to_thread discipline
botcbot/slugs.py          what a custom id may look like, kept in step with the fork
botcbot/discord_app.py    the slash commands, autocomplete and message formatting
botcbot/tls.py            OS trust store, installed before aiohttp is imported
tools/live_check.py       exercises the client against a real instance, no token needed
tests/                    offline unit tests
```

The only thing written to disk at runtime is the suggestion cache described above. PDFs
and rendered pages never touch it: they live in `io.BytesIO` and go straight to
`discord.File`.

`rendering.py` and `cache.py` are both blocking by design — PDFium and `sqlite3` have no
async API — so both are called through `asyncio.to_thread` and neither imports `discord`.

## Development

```bash
pip install -r requirements-dev.txt

ruff check .
pytest

# Exercise the client against a real instance — no Discord token required. Checks the
# /json path, the /script path and the search behind autocomplete, separately, and
# prints each script's custom id. Read-only: it never writes to your instance.
python tools/live_check.py
python tools/live_check.py "Sects and Violets" 13108
BOTC_BASE_URL=https://scripts.example.com python tools/live_check.py sects
```

The tests are entirely offline: `tests/conftest.py` fakes the `aiohttp` session and the
Discord interaction, and builds real PDFs with Pillow rather than shipping fixture files.

## Licence note

The renderer is `pypdfium2` (BSD-3-Clause / Apache-2.0) rather than the more commonly
suggested PyMuPDF. PyMuPDF is AGPL-3.0, whose §13 network clause is awkward for a bot that
other people interact with over a network, and escaping it means buying a commercial
licence. `pypdfium2` carries no such obligation, so this project stays freely
redistributable.
