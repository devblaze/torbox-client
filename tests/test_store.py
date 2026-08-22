import time

from app.store import STATE_COMPLETED, STATE_QUEUED, Torrent


def test_upsert_get_roundtrip(store):
    t = Torrent(hash="A" * 40, name="Movie", category="radarr", size=100, torbox_id=5,
                files=[{"id": 1, "name": "movie.mkv", "size": 100}])
    store.upsert(t)
    # Lookup is case-insensitive on the hash.
    got = store.get("a" * 40)
    assert got is not None
    assert got.name == "Movie"
    assert got.torbox_id == 5
    assert got.files == [{"id": 1, "name": "movie.mkv", "size": 100}]


def test_upsert_updates_existing(store):
    h = "b" * 40
    store.upsert(Torrent(hash=h, name="v1", state=STATE_QUEUED))
    store.upsert(Torrent(hash=h, name="v2", state=STATE_COMPLETED))
    got = store.get(h)
    assert got.name == "v2"
    assert got.state == STATE_COMPLETED
    assert len(store.all()) == 1


def test_delete(store):
    h = "c" * 40
    store.upsert(Torrent(hash=h, name="x"))
    store.delete(h)
    assert store.get(h) is None


def test_categories(store):
    store.set_category("radarr", "/downloads/radarr")
    store.set_category("radarr", "/downloads/movies")  # upsert
    assert store.categories() == {"radarr": "/downloads/movies"}
    store.remove_category("radarr")
    assert store.categories() == {}


def test_history_records_and_is_bounded(store):
    for i in range(1200):
        store.add_event(f"h{i}", f"name{i}", "radarr", "added", detail="d", size=i)
    events = store.history(5000)
    assert len(events) == store._HISTORY_LIMIT
    # Newest first.
    assert events[0]["name"] == "name1199"


def test_progress_property_stages():
    from app.store import STATE_CLOUD, STATE_DOWNLOADING
    t = Torrent(hash="d" * 40, name="x", state=STATE_CLOUD, cloud_progress=1.0)
    assert t.progress <= 0.90  # cloud phase is capped so Sonarr never imports early
    t.state = STATE_DOWNLOADING
    t.local_progress = 0.5
    assert 0.90 < t.progress < 1.0
    t.state = STATE_COMPLETED
    assert t.progress == 1.0


def _seed_history(store):
    """Six events across two categories and three kinds, oldest first."""
    rows = [
        ("h1", "Alpha Movie 2020", "radarr", "added", "magnet"),
        ("h2", "Beta Show S01E01", "sonarr", "added", "magnet"),
        ("h3", "Alpha Movie 2020", "radarr", "downloaded", "3 files"),
        ("h4", "Gamma Movie", "", "error", "cloud download failed"),
        ("h5", "Beta Show S01E02", "sonarr", "downloaded", "1 file"),
        ("h6", "Delta Show S02E01", "sonarr", "removed", ""),
    ]
    for i, (h, name, cat, event, detail) in enumerate(rows):
        store.add_event(h, name, cat, event, detail=detail, size=(i + 1) * 100)


def test_history_page_paginates_newest_first(store):
    _seed_history(store)
    first, total = store.history_page(limit=2, offset=0)
    assert total == 6
    assert [e["name"] for e in first] == ["Delta Show S02E01", "Beta Show S01E02"]

    second, total = store.history_page(limit=2, offset=2)
    assert total == 6  # the total is of matches, not of the page
    assert [e["name"] for e in second] == ["Gamma Movie", "Alpha Movie 2020"]

    beyond, total = store.history_page(limit=2, offset=99)
    assert beyond == [] and total == 6


def test_history_page_search_covers_name_detail_and_hash(store):
    _seed_history(store)
    by_name, total = store.history_page(search="beta")  # case-insensitive
    assert total == 2 and all("Beta" in e["name"] for e in by_name)

    by_detail, total = store.history_page(search="cloud download failed")
    assert total == 1 and by_detail[0]["name"] == "Gamma Movie"

    by_hash, total = store.history_page(search="h3")
    assert total == 1 and by_hash[0]["event"] == "downloaded"


def test_history_page_filters(store):
    _seed_history(store)
    _, total = store.history_page(events=["added"])
    assert total == 2
    _, total = store.history_page(events=["added", "error"])
    assert total == 3
    _, total = store.history_page(categories=["sonarr"])
    assert total == 3
    # An empty string in the list selects the uncategorised events.
    rows, total = store.history_page(categories=[""])
    assert total == 1 and rows[0]["name"] == "Gamma Movie"
    # Filters compose.
    _, total = store.history_page(categories=["sonarr"], events=["downloaded"])
    assert total == 1
    _, total = store.history_page(search="show", events=["added"], categories=["sonarr"])
    assert total == 1


def test_history_page_since_filters_by_age(store):
    _seed_history(store)
    now = int(time.time())
    _, total = store.history_page(since=now - 60)
    assert total == 6
    _, total = store.history_page(since=now + 60)  # everything is older than this
    assert total == 0


def test_history_page_sorting(store):
    _seed_history(store)
    names = lambda **kw: [e["name"] for e in store.history_page(limit=10, **kw)[0]]

    assert names(sort="name", order="asc")[0].startswith("Alpha")
    assert names(sort="name", order="desc")[0].startswith("Gamma")
    assert names(sort="ts", order="asc")[0] == "Alpha Movie 2020"  # oldest first
    sizes = [e["size"] for e in store.history_page(limit=10, sort="size", order="asc")[0]]
    assert sizes == sorted(sizes)
    # An unknown sort key falls back to chronological rather than erroring.
    assert names(sort="'; DROP TABLE history; --") == names(sort="ts")


def test_history_facets_lists_distinct_values(store):
    _seed_history(store)
    facets = store.history_facets()
    assert facets["events"] == ["added", "downloaded", "error", "removed"]
    # "" is present so the UI can offer an "uncategorised" filter.
    assert facets["categories"] == ["", "radarr", "sonarr"]


def test_history_count_ignores_filters(store):
    _seed_history(store)
    assert store.history_count() == 6


def test_history_retention_follows_the_runtime_setting(store, monkeypatch):
    from app import runtime

    monkeypatch.setitem(runtime._values, "history_retention", 150)
    for i in range(400):
        store.add_event(f"x{i}", f"name{i}", "radarr", "added")
    # Pruning is amortised to every hundredth insert, so the cap holds at the
    # last prune point rather than exactly at 150.
    assert store.history_count() <= 250

    monkeypatch.setitem(runtime._values, "history_retention", 20)
    removed = store.prune_history()
    assert removed > 0
    assert store.history_count() == 20
    assert store.history(50)[0]["name"] == "name399"  # newest survive
