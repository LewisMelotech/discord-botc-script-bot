# The Blood on the Clocktower Discord bot.
#
# Built by the Compose stack in ../botc-stack with `context: ../botc-discord-bot`, so this
# directory stays exactly where it is and is never copied into the stack repo.
#
# python:3.13-slim is enough: pyproject.toml requires >=3.11 and every dependency resolves
# to a wheel on both arm64 and amd64 — pypdfium2 bundles PDFium, so there are no system
# packages to install and no compiler needed.

FROM python:3.13-slim-bookworm

# BOTC_CACHE_PATH is the autocomplete cache. It must land on a volume, or the SQLite file
# (and its -wal/-shm sidecars) is lost every time the container is recreated.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    BOTC_CACHE_PATH=/data/botc-suggestions.sqlite3

WORKDIR /app

# Requirements first so the dependency layer caches across source edits.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

# Create the mount point before dropping privileges: Docker seeds an empty named volume
# from the image's ownership at that path, so /data comes up writable by `bot`.
RUN useradd -m -u 10001 bot \
 && mkdir -p /data \
 && chown -R bot:bot /data

USER bot

# `python bot.py` exits 2 when DISCORD_TOKEN is missing, which Compose surfaces as a
# clean Exited(2) without affecting the app or database.
CMD ["python", "bot.py"]
