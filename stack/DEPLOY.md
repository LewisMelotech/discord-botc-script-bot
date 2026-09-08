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

Two arrangements, depending on where your proxy runs.

**Proxy on the host** (nginx or Caddy installed directly): leave `APP_BIND=127.0.0.1` and
point it at `127.0.0.1:8000`. The port stays closed to the outside.

**Proxy in a container** (Traefik, nginx-proxy-manager, and anything managed by Dockge):
`127.0.0.1` inside that container is itself, not the host, so loopback will not work. Either
publish on the Docker bridge with `APP_BIND=0.0.0.0` and forward to the host's IP, or — better
— put both on one Docker network so it can use the service name. For the latter, add to
`docker-compose.yml`:

```yaml
services:
  botc-scripts:
    networks: [botc, proxy]

networks:
  proxy:
    external: true
    name: <your proxy's network>
```

Then forward to `http://botc-scripts:8000` and drop the published port entirely.

## 4. Bring it up

```sh
docker compose build
docker compose up -d
docker compose logs -f init
```

`init` runs migrations, loads the 172 characters and creates the admin and bot accounts,
then exits 0. It is idempotent, so restarts are safe.

Check it: `curl -H 'Host: scripts.example.com' http://127.0.0.1:8000/health-check` → 200.

## 5. Afterwards

- Log into `/admin` as your superuser and confirm the **Site** record (Sites → the one entry)
  is your domain, not `example.com`. Social login matches its provider app against it.
- Import a few scripts — a fresh instance starts empty. See `IMPORTING.md` in the fork.
- The bot registers slash commands globally, which can take an hour. Set `DISCORD_GUILD_ID`
  for instant registration in one server while you check it works.

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
  hides the upload form from everyone but staff. Importing is separately limited to the
  instances in `IMPORT_SOURCES`, and nothing rate-limits either form.
- **Signup is open.** `LOCAL_SIGNUP_ENABLED=False` closes registration while leaving login
  working, so you can create accounts yourself and hand out reset links (`ACCOUNTS.md`).
- **No email is sent**, so a forgotten password needs an admin-generated reset link.
