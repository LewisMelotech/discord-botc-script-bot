# Deploying to a server

This assumes a Linux host with Docker, a TLS-terminating reverse proxy you already run,
and a domain pointing at it. The stack itself terminates nothing and publishes no
certificate — it just serves HTTP on a port your proxy forwards to.

## 1. Get the compose file onto the host

Use **`docker-compose.deploy.yml`**. It pulls both images from GHCR, published by GitHub
Actions on every push, so there is nothing to clone and nothing compiles on the server.
Save it as `compose.yaml` (or paste it into Dockge) and that is the whole deployment.

The images are `ghcr.io/lewismelotech/slug-status` (built from `custom-slugs`) and
`ghcr.io/lewismelotech/discord-botc-script-bot` (from `main`). Both must be **public**
packages, or the host needs `docker login ghcr.io` — see "First publish" below.

Cloning the two repos side by side is only needed for `docker-compose.yml`, which builds
from local checkouts and is meant for development:

```sh
git clone https://github.com/LewisMelotech/discord-botc-script-bot.git
git clone -b custom-slugs https://github.com/LewisMelotech/slug-status.git botc-scripts
cd discord-botc-script-bot/stack
```

## 2. Write the .env

```sh
cp .env.example .env
```

Generate **fresh** secrets — never reuse the ones from a laptop:

```sh
python3 -c "import secrets; print('SECRET_KEY=' + secrets.token_urlsafe(64))"
python3 -c "import secrets; print('POSTGRES_PASSWORD=' + secrets.token_urlsafe(24))"
```

Set at minimum:

| Variable | Value |
|---|---|
| `SECRET_KEY` | freshly generated |
| `POSTGRES_PASSWORD` | freshly generated |
| `DJANGO_SUPERUSER_PASSWORD` | your admin password |
| `BOTC_API_PASSWORD` | the bot's API account password |
| `DISCORD_TOKEN` | from the Discord developer portal |
| `DJANGO_HOST` | `scripts.example.com botc-scripts localhost 127.0.0.1` |
| `CSRF_TRUSTED_ORIGINS` | `https://scripts.example.com` |
| `BEHIND_TLS_PROXY` | `True` |

`DJANGO_HOST` must keep `botc-scripts` in the list: that is the Host header the bot sends
over the internal network, and Django answers 400 to anything unlisted. `CSRF_TRUSTED_ORIGINS`
must be the `https://` origin or every login and upload POST fails. `BEHIND_TLS_PROXY=True`
trusts `X-Forwarded-Proto` and marks cookies secure — set it only because a proxy really is
in front, since over plain HTTP it locks you out.

## 3. Let your proxy reach the app

The deploy file publishes the app on host port **8480** (`APP_PORT` changes it) on every
interface (`APP_BIND=0.0.0.0`), because a proxy that is itself a container cannot reach the
host's loopback. Two arrangements, depending on where your proxy runs.

**Proxy in a container** (Traefik, nginx-proxy-manager, and anything managed by Dockge):
the default works. Forward to the host's LAN address on `8480`, scheme `http`. Because the
port is open on every interface, anything that can route to the host can also reach the app
directly and skip the proxy and its TLS, so firewall the port to the proxy. Or, better, put
both on one Docker network so the proxy can use the service name: uncomment the `proxy`
network at the bottom of `docker-compose.deploy.yml`, and add it to the app:

```yaml
services:
  botc-scripts:
    networks: [botc, proxy]

networks:
  proxy:
    external: true
    name: <your proxy's network>
```

Then forward to `http://botc-scripts:8000` and drop the `ports:` block entirely.

**Proxy on the host** (nginx or Caddy installed directly): set `APP_BIND=127.0.0.1` and
point it at `127.0.0.1:8480`. The port stays closed to the outside.

## 4. Bring it up

```sh
docker compose pull
docker compose up -d
docker compose logs -f init
```

`init` runs migrations, loads the 172 characters and creates the admin and bot accounts,
then exits 0. It is idempotent, so restarts are safe.

The `sync` service then pulls new versions of linked scripts once a day at 09:00 UK time.
`SYNC_AT` and `SYNC_TIMEZONE` change when; `docker compose logs sync` shows each run and
when the next one is due.

Check it: `curl -H 'Host: scripts.example.com' http://127.0.0.1:8480/health-check` → 200.

## 5. Afterwards

- Log into `/admin` as your superuser and confirm the **Site** record (Sites → the one entry)
  is your domain, not `example.com`. Social login matches its provider app against it.
- Import a few scripts — a fresh instance starts empty. See `IMPORTING.md` in the fork.
- The bot registers slash commands globally, which can take an hour. Set `DISCORD_GUILD_ID`
  for instant registration in one server while you check it works.
- Optional: point `DISCORD_ARRIVALS_WEBHOOK_URL` at a channel to be told when a script
  arrives that might need putting on the Minecraft server, and `DISCORD_ONLINE_WEBHOOK_URL`
  at one to be told when a version goes on it — the same channel or separate ones.
  `SITE_URL=https://scripts.example.com` makes the announcements link back. Verify every
  configured webhook at once with
  `docker compose exec botc-scripts python manage.py test_notification`. See
  `NOTIFICATIONS.md` in the fork.

## First publish

GHCR packages start **private**. After the first successful Actions run, open each
package (GitHub → your profile → Packages), Package settings → Change visibility →
Public. Otherwise the host cannot pull and Dockge reports "denied".

Alternatively keep them private and run `docker login ghcr.io` on the host with a
personal access token holding `read:packages`.

## Updating

Push to the branch, wait for Actions to go green, then press **Update** in Dockge — or:

```sh
docker compose pull && docker compose up -d
```

`init` re-runs migrations before the app starts, so schema changes apply themselves.

`:latest` follows the branch, so Update always takes the newest build. To hold or roll
back to a known-good one, pin the sha tag that Actions also publishes:

```sh
APP_IMAGE=ghcr.io/lewismelotech/slug-status:sha-1a2b3c4
BOT_IMAGE=ghcr.io/lewismelotech/discord-botc-script-bot:sha-1a2b3c4
```

## Backups

Everything durable is in three named volumes: `botc_pgdata` (the database),
`botc_appdata` (uploaded PDFs) and `botc_botdata` (the bot's autocomplete cache, which is
disposable). Back up the first two:

```sh
docker compose exec -T db pg_dump -U botc botc | gzip > botc-$(date +%F).sql.gz
docker run --rm -v botc_appdata:/data -v "$PWD:/out" alpine \
  tar czf /out/botc-media-$(date +%F).tar.gz -C /data .
```

Restoring the database needs the stack up and the app stopped, so nothing writes while it
loads:

```sh
docker compose stop botc-scripts bot
gunzip -c botc-2026-09-07.sql.gz | docker compose exec -T db psql -U botc botc
docker compose start botc-scripts bot
```

## Things to decide before opening it up

- **Uploads and imports are open to anonymous visitors**, as upstream is. `UPLOAD_DISABLED=True`
  only greys out the Submit button on the upload page for everyone but staff. It is a
  courtesy, not a lock: the upload view and the API do not check it, so a hand-made POST
  still goes through. Importing is separately limited to the instances in `IMPORT_SOURCES`,
  and nothing rate-limits either form. Adding a version to a script someone owns needs that
  owner or staff (`ACCOUNTS.md`).
- **Signup is open.** `LOCAL_SIGNUP_ENABLED=False` closes username/password registration
  and `SOCIAL_SIGNUP_ENABLED=False` closes registration through Discord or Google. Each
  leaves login working, so you can allow one kind, or neither and create accounts
  yourself and hand out reset links (`ACCOUNTS.md`).
- **No email is sent**, so a forgotten password needs an admin-generated reset link.
