from __future__ import annotations

import sqlite3
import threading

from botcbot.cache import ScriptCache


def cache(tmp_path, **kwargs) -> ScriptCache:
    return ScriptCache(tmp_path / "suggestions.sqlite3", **kwargs)


def test_a_fresh_database_and_its_parent_directory_are_created_on_first_write(tmp_path):
    store = ScriptCache(tmp_path / "nested" / "dir" / "suggestions.sqlite3")
    assert not store.path.exists()
    store.record(script_id=1, name="Trouble Brewing")
    assert store.path.exists()
    assert [entry.name for entry in store.suggest("trouble")] == ["Trouble Brewing"]
    store.close()


def test_serving_the_same_script_again_refreshes_it_instead_of_duplicating_it(tmp_path):
    store = cache(tmp_path)
    store.record(script_id=1, name="Trouble Brewing", guild_id=7, used_at=100.0)
    store.record(script_id=2, name="Sects and Violets", guild_id=7, used_at=200.0)
    store.record(script_id=1, name="Trouble Brewing", guild_id=7, used_at=300.0)

    assert store.count() == 2
    assert [entry.script_id for entry in store.suggest("", guild_id=7)] == [1, 2]
    store.close()


def test_the_same_script_in_two_guilds_is_offered_once_preferring_this_guild(tmp_path):
    store = cache(tmp_path)
    store.record(script_id=1, name="Trouble Brewing", guild_id=1, used_at=100.0)
    store.record(script_id=2, name="Trouble At The Mill", guild_id=2, used_at=200.0)
    store.record(script_id=1, name="Trouble Brewing", guild_id=2, used_at=50.0)

    # Guild 2 holds an older row for script 1, but its own rows still come first.
    assert [entry.script_id for entry in store.suggest("trouble", guild_id=2)] == [2, 1]
    assert [entry.script_id for entry in store.suggest("trouble", guild_id=1)] == [1, 2]
    store.close()


def test_a_guildless_invocation_still_sees_everything_the_bot_has_served(tmp_path):
    store = cache(tmp_path)
    store.record(script_id=1, name="Trouble Brewing", guild_id=99, used_at=100.0)
    assert [entry.script_id for entry in store.suggest("trouble", guild_id=None)] == [1]
    store.close()


def test_matching_is_case_and_accent_folded_rather_than_ascii_only(tmp_path):
    store = cache(tmp_path)
    store.record(script_id=1, name="STRASSE der Träume")
    assert [entry.script_id for entry in store.suggest("träume")] == [1]
    assert [entry.script_id for entry in store.suggest("STRASSE")] == [1]
    assert store.suggest("nothing here") == []
    store.close()


def test_an_empty_query_returns_the_most_recently_served_scripts(tmp_path):
    store = cache(tmp_path)
    for index in range(5):
        store.record(script_id=index, name=f"Script {index}", used_at=float(index))
    assert [entry.script_id for entry in store.suggest("", limit=3)] == [4, 3, 2]
    store.close()


def test_the_table_is_bounded_by_evicting_the_oldest_entries(tmp_path):
    store = cache(tmp_path, max_entries=3)
    for index in range(10):
        store.record(script_id=index, name=f"Script {index}", used_at=float(index))

    assert store.count() == 3
    assert [entry.script_id for entry in store.suggest("")] == [9, 8, 7]
    store.close()


def test_eviction_keeps_a_script_that_was_refreshed_rather_than_its_insertion_order(tmp_path):
    store = cache(tmp_path, max_entries=2)
    store.record(script_id=1, name="Old", used_at=1.0)
    store.record(script_id=2, name="Middle", used_at=2.0)
    store.record(script_id=1, name="Old", used_at=3.0)
    store.record(script_id=3, name="New", used_at=4.0)

    assert [entry.script_id for entry in store.suggest("")] == [3, 1]
    store.close()


def test_zero_entries_disables_the_cache_without_creating_a_database(tmp_path):
    store = cache(tmp_path, max_entries=0)
    store.record(script_id=1, name="Trouble Brewing")
    assert not store.enabled
    assert store.suggest("trouble") == []
    assert store.count() == 0
    assert not store.path.exists()
    store.close()


def test_a_non_positive_limit_asks_for_nothing_rather_than_everything(tmp_path):
    store = cache(tmp_path)
    store.record(script_id=1, name="Trouble Brewing")
    assert store.suggest("", limit=0) == []
    store.close()


def test_a_custom_id_is_stored_and_matched_as_well_as_the_name(tmp_path):
    store = cache(tmp_path)
    store.record(script_id=13108, name="Sects and Violets", slug="snv")
    store.record(script_id=42, name="Catfishing")

    (entry,) = store.suggest("snv")
    assert (entry.script_id, entry.slug) == (13108, "snv")
    # A row with no custom id simply fails that half of the match rather than matching
    # everything: instr(NULL, ...) is NULL in SQLite, so no COALESCE is needed.
    assert [e.script_id for e in store.suggest("catfishing")] == [42]
    assert store.suggest("nothing here") == []
    store.close()


def test_serving_a_script_again_refreshes_a_custom_id_it_has_since_been_given(tmp_path):
    store = cache(tmp_path)
    store.record(script_id=1, name="Sects and Violets", used_at=1.0)
    store.record(script_id=1, name="Sects and Violets", slug="snv", used_at=2.0)

    assert store.count() == 1
    assert [e.slug for e in store.suggest("snv")] == ["snv"]
    store.close()


def test_a_database_written_before_custom_ids_is_migrated_rather_than_broken(tmp_path):
    # CREATE TABLE IF NOT EXISTS is a no-op on a table that already exists, so a column
    # added to the schema never reaches a database already on disk, and every later
    # INSERT naming it would raise. Migrating keeps the suggestions already accumulated.
    path = tmp_path / "suggestions.sqlite3"
    old = sqlite3.connect(path)
    with old:
        old.execute(
            """
            CREATE TABLE served_scripts (
                id        INTEGER PRIMARY KEY,
                script_id INTEGER NOT NULL,
                guild_id  INTEGER NOT NULL,
                name      TEXT    NOT NULL,
                name_fold TEXT    NOT NULL,
                author    TEXT,
                used_at   REAL    NOT NULL
            )
            """
        )
        old.execute(
            "CREATE UNIQUE INDEX served_scripts_key ON served_scripts (script_id, guild_id)"
        )
        old.execute(
            "INSERT INTO served_scripts (script_id, guild_id, name, name_fold, author, used_at)"
            " VALUES (1, 0, 'Trouble Brewing', 'trouble brewing', 'TPI', 1.0)"
        )
    old.close()

    store = cache(tmp_path)
    store.record(script_id=2, name="Sects and Violets", slug="snv", used_at=2.0)

    assert [(e.script_id, e.slug) for e in store.suggest("")] == [(2, "snv"), (1, None)]
    store.close()


def test_the_author_is_kept_for_labelling_but_the_script_json_is_never_stored(tmp_path):
    store = cache(tmp_path)
    store.record(script_id=1, name="Trouble Brewing", author="The Pandemonium Institute")

    entry = store.suggest("trouble")[0]
    assert entry.author == "The Pandemonium Institute"

    with sqlite3.connect(store.path) as raw:
        columns = {row[1] for row in raw.execute("PRAGMA table_info(served_scripts)")}
    assert "content" not in columns
    store.close()


def test_many_threads_can_read_and_write_at_once_without_raising(tmp_path):
    store = cache(tmp_path, max_entries=50)
    failures: list[BaseException] = []
    start = threading.Barrier(8)

    def worker(worker_id: int) -> None:
        start.wait()
        try:
            for step in range(40):
                store.record(
                    script_id=worker_id * 100 + step,
                    name=f"Script {worker_id}-{step}",
                    guild_id=worker_id,
                )
                store.suggest("script", guild_id=worker_id)
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not failures, failures
    assert store.count() == 50
    store.close()


def test_the_connection_is_configured_for_concurrent_use(tmp_path):
    store = cache(tmp_path)
    store.record(script_id=1, name="Trouble Brewing")
    with sqlite3.connect(store.path) as raw:
        (mode,) = raw.execute("PRAGMA journal_mode").fetchone()
    # WAL is what lets an autocomplete read overlap a delivery's write.
    assert mode.lower() == "wal"
    store.close()
