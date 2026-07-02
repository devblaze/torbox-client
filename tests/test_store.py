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
