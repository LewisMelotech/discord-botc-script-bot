# botc-stack

A self-hosted [botc-scripts](https://github.com/AdmiralGT/botc-scripts) instance and the
Blood on the Clocktower Discord bot, packaged together with Docker Compose.

The point of self-hosting is **custom script slugs**: short, memorable, permanent ids like
`sects` instead of `13108`. Slugs are a real field on the `Script` model in this fork, so
they work in the web UI (`/script/sects`), in the API, and — once the `/alias` command
lands — from Discord.

---

## Topology

| Service | Image | Role | Published? |
|---|---|---|---|
| `db` | `postgres:17-bookworm` | The only stateful service. | No — internal only. |
| `init` | `botc-scripts:local` | One-shot, idempotent: migrate → load characters → create accounts. Exits 0. | No |
| `botc-scripts` | built from `../../botc-scripts` | The Django app under gunicorn. | `127.0.0.1:8000` by default |
| `bot` | built from `..` (this repo) | The Discord bot. | No |

All four sit on a user-defined bridge network called `botc`, which is what gives them
service-name DNS. The bot reaches the app at **`http://botc-scripts:8000`** — it never uses
the published host port, so you can change or remove that port without affecting the bot.

Three named volumes hold everything that must survive a restart:

| Volume | Mounted at | Contents |
|---|---|---|
| `botc_pgdata` | `db:/var/lib/postgresql/data` | The database. |
| `botc_appdata` | `botc-scripts:/data` | Uploaded script PDFs (`/data/public/media`) and the bootstrap-icon cache. |
| `botc_botdata` | `bot:/data` | The bot's SQLite autocomplete cache. |

Static CSS/JS is deliberately **not** on a volume — it is built into the image by
`collectstatic` at build time and served by WhiteNoise, so there is no nginx sidecar and no
shared static volume to go stale.

---

## Requirements

- Docker Engine 25+ with Compose v2+ (developed against Docker 29.5 / Compose v5.1).
- The two checkouts side by side, because Compose builds both in place rather than
  copying anything in here:

  ```
  discord-botc-script-bot/    this repo — the bot, with this stack in stack/
  botc-scripts/               the fork, a sibling of this repo
  ```

  ```sh
  git clone https://github.com/LewisMelotech/discord-botc-script-bot.git
  git clone https://github.com/LewisMelotech/botc-scripts.git
  cd discord-botc-script-bot/stack
  ```

  The fork must be on the `custom-slugs` branch: it carries the slug field, the local
  account login and the container packaging, none of which are upstream.
- About 1.5 GB of disk for the images.

---

## Bringing it up from scratch

```sh
cp .env.example .env
```

Now edit `.env`. Two values are **required** and have no defaults anywhere in this repo —
the stack refuses to start rather than run on a guessable secret:

```sh
# Django's signing key
python3 -c "import secrets; print(secrets.token_urlsafe(64))"
# The Postgres password
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
```

Two more are worth setting on the first boot, because they decide which accounts get
created (see [First boot](#what-actually-happens-on-first-boot)):

```ini
DJANGO_SUPERUSER_PASSWORD=...   # for /admin. Blank = no superuser is created.
BOTC_API_PASSWORD=...           # the account the bot writes slugs as. Blank = no API user.
```

`DISCORD_TOKEN` can stay blank for now. Then:

```sh
docker compose build
docker compose up -d
```

A cold boot takes about 20 seconds; a restart with the volumes already populated, about 15.
The site is at <http://127.0.0.1:8000> and the admin at <http://127.0.0.1:8000/admin>.

```sh
docker compose ps          # everything should be Up (healthy), except init: Exited (0)
docker compose logs init   # what the first boot actually did
```

`init` exiting **0** is the success signal. `botc-scripts` will not start until it does.

---

## What actually happens on first boot

`init` is a separate one-shot service rather than an entrypoint script. That is deliberate:
`depends_on: service_completed_successfully` gives a hard ordering guarantee, three gunicorn
workers cannot race each other running `migrate`, and a failure shows up as a non-zero exit
instead of being buried in application logs.

It runs three commands, and **all three are safe to repeat on every boot**:

1. `manage.py migrate --noinput` — second run prints `No migrations to apply.`
2. `manage.py loaddata dev/characters` — 172 official characters. The fixture carries
   explicit primary keys, so it is an upsert, not a duplicate insert. Takes about a second,
   and re-running keeps character data current after you rebase the fork.
3. `manage.py bootstrap` — creates the admin and bot accounts (below). Second run prints
   `exists: admin` / `exists: discordbot` and grants no duplicate permissions.

Static files are not in that list because they are already built into the image.

You will see three allauth warnings (`account.W001` and two deprecations) in the init log on
every boot. They are upstream's, not this stack's, and are harmless.

### Ordering, and why it is trustworthy

```
db Started → db Healthy → init Started → init Exited(0) → botc-scripts Started
  → botc-scripts Healthy → bot Started
```

Two details make that chain mean what it says:

- The Postgres healthcheck is `pg_isready -h 127.0.0.1`, not a plain `pg_isready`. During
  `initdb` the official entrypoint runs a temporary server with `listen_addresses=''`, which
  answers on the unix socket and would report "accepting connections" while the database is
  still initialising. Only the TCP check tells the truth.
- The app's `/health-check` is a **liveness** check only — the view returns an empty 200 and
  never touches the database. It stays 200 even with the database stopped. What actually
  proves the database is usable is `init` having exited 0, which is why the app depends on
  both.

---

## What a fresh instance contains — read this before you migrate anything

**Zero scripts. Zero script versions. Zero tags. No votes, favourites, comments or
collections.**

The public site at botcscripts.com currently hosts over 11,000 scripts. A self-hosted
instance starts with none of them, and there is **no built-in import path**. Upstream's own
README says as much: you will need to upload your own scripts.

What you do get:

| | Fresh instance |
|---|---|
| Official characters | **172**, from `dev/characters.json`, loaded on every boot |
| Scripts / versions | **0** |
| Script tags | **0** |
| Users | The admin and bot accounts you configured, and nothing else |

Two consequences worth knowing up front:

- **Numeric script ids will not match the public site's.** Your first upload is script `1`.
  Anything you have that is keyed on a public numeric id is invalidated by self-hosting.
  This is the strongest argument for slugs: a slug is an id you choose, so it survives the
  move and any future re-import.
- **The tag filter is empty until you create tags by hand** (admin → Script tags).
  `ScriptTag.order` is unique and non-nullable, so tags cannot be auto-created. Until they
  exist, the API's automatic "Hybrid"/"Homebrew" tagging silently does nothing — it catches
  the missing-tag error and moves on. Nothing crashes; the tags just never appear.

To get scripts in, either use the upload form at `/script/upload`, or POST them to the API
(below). A bulk importer that reads the public API and writes to yours is perfectly feasible
— roughly 224 pages at 50 scripts each — but it does not exist here, it cannot carry PDFs,
owners, votes, favourites, comments or tags, and hammering someone else's site for 11,000
records is a courtesy question worth raising with the upstream maintainer first.

### Characters

Loaded automatically on every boot. To reload by hand after changing the fixture:

```sh
docker compose exec botc-scripts python manage.py loaddata dev/characters
```

---

## Accounts

`manage.py bootstrap` (in the fork, at
`botc-scripts/scripts/management/commands/bootstrap.py`) creates two accounts, and
**invents no passwords**. If the relevant password variable is blank it skips that account
and prints why, rather than creating something guessable that gets committed to a repo or
forgotten in production.

Passwords are only ever set when an account is first created. To force-reset both to the
values in `.env`, set `BOOTSTRAP_RESET_PASSWORDS=True` for one boot, then set it back.

### The admin

From `DJANGO_SUPERUSER_USERNAME` (default `admin`) and `DJANGO_SUPERUSER_PASSWORD`. If you
left the password blank, create one interactively whenever you like:

```sh
docker compose exec botc-scripts python manage.py createsuperuser
```

### The API user the bot writes slugs as

From `BOTC_API_USERNAME` (default `discordbot`) and `BOTC_API_PASSWORD`.

It is granted exactly one permission — `scripts.api_write_permission` — and is neither
staff nor superuser, so it cannot log into the admin. That single permission is what the
app checks on every API write, including the slug endpoints.

The app authenticates it with **HTTP Basic**. There is no token model: `rest_framework.
authtoken` is not installed, and DRF's default authentication classes (Session + Basic) are
what the write endpoints fall back on.

To confirm it exists and is correctly scoped:

```sh
docker compose exec botc-scripts python manage.py shell -c "
from django.contrib.auth import get_user_model
u = get_user_model().objects.get(username='discordbot')
print(u.username, 'staff:', u.is_staff, 'can write:', u.has_perm('scripts.api_write_permission'))"
```

### The slug API

Reads are anonymous; writes need Basic auth as the account above.

| Operation | Request |
|---|---|
| Look up by slug | `GET /api/script_ids/slug/<slug>/` |
| Filter by slug | `GET /api/script_ids/?slug=<slug>` |
| Set a slug | `PATCH /api/script_ids/<pk>/slug/` with `{"slug": "sects"}` |
| Clear a slug | `PATCH` with `{"slug": null}`, or `DELETE` the same path |

Slugs are lowercase letters, digits and single internal hyphens, 2–50 characters. Input is
trimmed and case-folded, so `  SECTS  ` is stored as `sects` and `/script/SECTS` resolves.
Two rules exist to stop slugs colliding with script ids and site URLs: a slug may not parse
as a number (`13108` is rejected), and may not be a reserved word (`search`, `upload`,
`api`, `admin`, …). Full contract: `botc-scripts/SLUGS.md`.

---

## Pointing the bot at the app

Nothing to configure — Compose already sets it:

```yaml
BOTC_BASE_URL: http://botc-scripts:8000
BOTC_CACHE_PATH: /data/botc-suggestions.sqlite3
```

`BOTC_BASE_URL` is pinned in `docker-compose.yml` rather than `.env` on purpose: it is a
fact about the network topology, not a user preference. `BOTC_CACHE_PATH` must point at the
volume, or the SQLite cache is lost every time the container is recreated.

To actually run the bot, put a token in `.env` and recreate it:

```ini
DISCORD_TOKEN=...            # Discord Developer Portal → your app → Bot → Reset Token
DISCORD_GUILD_ID=...         # optional: registers slash commands instantly in one server
```

```sh
docker compose up -d bot
docker compose logs -f bot
```

**Without a token the stack still comes up completely.** The bot exits with code 2 and a
clear message, `restart: on-failure:3` lets it settle at `Exited (2)` instead of
crash-looping, and the database and app are entirely unaffected. That is the intended state
until you are ready.

### Verifying end to end without a Discord token

`tools/live_check.py` in the bot repo exercises the real client — resolution, the JSON
endpoint, PDF download and rasterisation — against whatever `BOTC_BASE_URL` points at. It
never constructs the bot's `Config`, so it needs no token:

```sh
docker compose run --rm bot python tools/live_check.py "Sects and Violets" 1 sects
```

This is the fastest way to prove the whole chain — database → model → API → serializer →
HTTP client — is healthy from inside the network, with only the Discord connection
unproven. On a fresh instance every query prints `NOT FOUND`, because there are no scripts
yet; that is correct, not a failure.

---

## Day-to-day

```sh
docker compose logs -f botc-scripts        # application log
docker compose ps                          # health at a glance
docker compose exec botc-scripts python manage.py shell
docker compose exec db psql -U botc -d botc
docker compose restart botc-scripts
docker compose down                        # stop everything; volumes are kept
```

After changing code in the fork or the bot:

```sh
docker compose build && docker compose up -d
```

`init` re-runs automatically and applies any new migrations. Note that if the fork ever
gains a Python dependency you must run `uv lock` in `botc-scripts/` — the image builds with
`uv sync --locked`, which fails loudly on a stale lockfile rather than drifting silently.

---

## Backups

Two things need backing up: the database, and the uploaded PDFs. Both commands below are
verified working.

```sh
mkdir -p backups

# Database → a compressed SQL dump
docker compose exec -T db pg_dump -U botc -d botc --clean --if-exists \
  | gzip > "backups/botc-$(date +%F).sql.gz"

# Uploaded PDFs and the icon cache → a tarball of the appdata volume
docker run --rm -v botc_appdata:/data:ro -v "$PWD/backups":/backup busybox \
  tar czf "/backup/media-$(date +%F).tar.gz" -C /data .
```

Restoring:

```sh
# Database (drops and recreates every object, because of --clean --if-exists)
gzip -dc backups/botc-2026-09-07.sql.gz | docker compose exec -T db psql -U botc -d botc

# Media
docker run --rm -v botc_appdata:/data -v "$PWD/backups":/backup busybox \
  tar xzf /backup/media-2026-09-07.tar.gz -C /data
```

Then `docker compose up -d` and let `init` run. It is idempotent, so restoring an older
dump and re-running migrations is safe — including the `pg_trgm` extension, which a restore
into a fresh volume would otherwise be missing.

The `.env` file is **not** covered by either backup and is gitignored. Keep `SECRET_KEY`
somewhere safe: changing it invalidates every session and password-reset link.

---

## Troubleshooting

**Search returns HTTP 500, everything else works.** The `pg_trgm` extension is missing. The
name and author search calls Postgres's `similarity()`, and upstream ships no migration that
creates the extension. This stack guarantees it twice: `db/initdb/10-pg_trgm.sql` runs while
the data directory is being initialised, and migration `0048_trigram_extension` in the fork
covers every other case — an existing volume, or a restored backup — because `initdb` scripts
only ever run on an empty data directory. To check:

```sh
docker compose exec db psql -U botc -d botc -c "SELECT extname FROM pg_extension;"
```

**Every page returns HTTP 400.** The `Host` header is not in `ALLOWED_HOSTS`. Add the name
you are using to `DJANGO_HOST` in `.env` (space-separated) and recreate the app. Keep
`botc-scripts` in that list — it is the Host header the bot sends internally, and dropping
it breaks the bot while leaving the browser working.

**`/health-check/` returns 404.** The route has no trailing slash. Use `/health-check`.
Django's `APPEND_SLASH` appends slashes, it never strips them.

**The bot is `Exited (2)`.** `DISCORD_TOKEN` is unset or empty. Expected — see above.

**Icons render as the literal text "Failed to read icon".** `django-bootstrap-icons` fetches
each SVG from a CDN the first time it is rendered and caches it in `/data/icon_cache`. The
container needs outbound HTTPS on first render. It degrades gracefully rather than erroring,
and the cache is on a volume, so this is a one-time cost per icon.

**A newly uploaded PDF 404s.** WhiteNoise builds its file map at startup, so
`WHITENOISE_AUTOREFRESH` must stay `True` — every script PDF is uploaded after boot. It is
on by default here.

**Port 8000 is already in use.** Set `APP_PORT` in `.env`. The bot is unaffected either way;
it uses the internal network, not the published port.

---

## Files

Two sibling checkouts. This stack lives inside the bot repo and reaches out to the fork.

```
discord-botc-script-bot/          the bot repo (build context for the bot service)
├── Dockerfile                    python:3.13-slim; built in place by Compose
├── bot.py, botcbot/, tests/      the bot itself
└── stack/                        <- you are here
    ├── docker-compose.yml        the four services, the network and the three volumes
    ├── .env.example              every variable, commented; copy to .env
    ├── README.md                 this file
    └── db/initdb/10-pg_trgm.sql  creates pg_trgm while the data directory initialises

botc-scripts/                     the fork, on the custom-slugs branch
├── Dockerfile                    uv + Python 3.13, collectstatic at build time
├── botc/docker.py                the container settings module
├── SLUGS.md                      the slug API contract
└── scripts/
    ├── adapters.py               gates local signup without closing login
    ├── management/commands/bootstrap.py    idempotent account creation
    └── migrations/
        ├── 0047_script_slug.py             the slug field
        └── 0048_trigram_extension.py       pg_trgm, for existing databases
```

The fork is deliberately a separate repository: it keeps `AdmiralGT/botc-scripts` history so
upstream changes can be pulled in, which vendoring it here would throw away. `.gitignore`
in this repo keeps `.env` and database backups out of git.

The fork is a separate git checkout with its own history and is gitignored here. If you want
it tracked, add it as a submodule — do not commit its contents into this directory.
