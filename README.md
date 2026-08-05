# torbox-client

Use **[TorBox](https://torbox.app)** as a download client for **Sonarr** and **Radarr** — the same way [rdt-client](https://github.com/rogerfar/rdt-client) lets you use Real-Debrid.

It pretends to be **qBittorrent**: your *arr apps send releases to it exactly as they would to a normal torrent client. Behind the scenes it hands each release to TorBox, waits for TorBox to download it in the cloud, then pulls the finished files down to your downloads share so Sonarr/Radarr import them normally.

```
Sonarr/Radarr ──(qBittorrent API)──▶ torbox-client ──(TorBox API)──▶ TorBox cloud
      ▲                                    │                              │
      └───────── imports from ◀── /downloads ◀──── pulls finished files ──┘
```

## How it works

1. Sonarr/Radarr grab a release and push the magnet/`.torrent` to this service (qBittorrent `torrents/add`).
2. It computes the real infohash (so *arr can track it) and sends it to TorBox.
3. A background worker polls TorBox for progress. Progress is reported to *arr but **capped below 100% until files are actually on local disk** — so nothing imports early.
4. When TorBox finishes, the worker downloads each file to `/downloads/<category>/…`.
5. Once every file is local, the torrent reports **completed** and Sonarr/Radarr import it (hardlink/move into your library).
6. After import, *arr removes it; the service deletes the local files and (optionally) the TorBox cloud torrent.

## Prerequisites

- A TorBox account + API key — from **https://torbox.app/settings** (API section).
- Sonarr and/or Radarr.
- **The single most important rule:** Sonarr/Radarr and this container must see the completed files at the **same path**. On Unraid that means mounting the same share (e.g. `/mnt/user/data`) at the same container path everywhere. This is what makes hardlink imports work.

---

## Install

### Option A — Unraid (recommended)

The image is published to GHCR and there's a ready-made Unraid template.

> **Note:** The **Template** dropdown on the *Add Container* page only lists templates that already exist on your server — you can't paste a URL into it. Use one of the two methods below to make the template appear there, or just fill in the fields by hand.

**Method 1 — install the template from URL (via SSH), then pick it from the dropdown:**

1. SSH into your Unraid server (or use the web terminal) and run:
   ```bash
   wget -O "/boot/config/plugins/dockerMan/templates-user/my-torbox-client.xml" \
     https://raw.githubusercontent.com/devblaze/torbox-client/main/templates/torbox-client.xml
   ```
2. Unraid → **Docker** tab → **Add Container**.
3. In the **Template** dropdown, under **User templates**, choose **torbox-client**. All fields pre-fill.
4. Adjust the values (see below), then **Apply**. The container starts on port **8080**.

**Method 2 — create the container manually:**

1. Unraid → **Docker** tab → **Add Container** (leave Template on *Select a template*).
2. Set:
   - **Repository:** `ghcr.io/devblaze/torbox-client:latest`
   - **Network Type:** `Bridge`
   - **Port:** add `8080` → `8080` (tcp)
   - **Path:** `/downloads` → your downloads share, e.g. `/mnt/user/data/downloads`
   - **Path:** `/data` → `/mnt/user/appdata/torbox-client`
   - **Variables** (Add another Path, Port, Variable...): `TORBOX_API_KEY`, `QBIT_USER`, `QBIT_PASS`, and optionally `PUID=99`, `PGID=100`.
3. **Apply**.

**Values to set either way:**
   - **TORBOX_API_KEY** — your key from torbox.app/settings.
   - **QBIT_USER / QBIT_PASS** — leave `admin` / `adminadmin` or change them (you'll type these into Sonarr/Radarr).
   - **Downloads** — set this to the **same share your Sonarr/Radarr use**, e.g. `/mnt/user/data/downloads` → `/downloads`.
   - **Appdata (state)** — `/mnt/user/appdata/torbox-client` → `/data`.
   - **PUID/PGID** — leave `99` / `100` (Unraid defaults).

### Option B — docker compose (any Docker host)

```bash
git clone https://github.com/devblaze/torbox-client.git
cd torbox-client
cp .env.example .env      # set TORBOX_API_KEY (and QBIT_USER/QBIT_PASS)
# edit docker-compose.yml: point the /downloads volume at the SAME host dir
# your Sonarr/Radarr use, and set PUID/PGID if needed.
docker compose up -d
docker compose logs -f    # should log: "TorBox API key validated"
```

### Option C — build locally

```bash
docker build -t torbox-client .
docker run -d --name torbox-client -p 8080:8080 \
  -e TORBOX_API_KEY=xxxx -e PUID=99 -e PGID=100 \
  -v /mnt/user/data/downloads:/downloads \
  -v /mnt/user/appdata/torbox-client:/data \
  torbox-client
```

Verify it's up: `curl http://<host>:8080/health` → `{"status":"ok"}`.

---

## Connecting Sonarr and Radarr

Do this in **each** app (Sonarr and Radarr). The only differences are the recommended category name.

### 1. Add the download client

**Settings → Download Clients → ➕ → qBittorrent**

| Field | Value |
|---|---|
| **Name** | `TorBox` |
| **Host** | the container/host running torbox-client. On Unraid or same Docker host, use the host's LAN IP (e.g. `192.168.1.10`) or the container name if on the same custom Docker network. |
| **Port** | `8080` |
| **Username** | `admin` (or your `QBIT_USER`) |
| **Password** | `adminadmin` (or your `QBIT_PASS`) |
| **Category** | Sonarr → `tv-sonarr`  •  Radarr → `radarr` |
| Use SSL | Off |

Click **Test** (should go green) → **Save**.

### 2. Completed Download Handling

**Settings → Download Clients** (bottom of page): make sure **Completed Download Handling → Enable** is **on** (it is by default). That's what triggers Sonarr/Radarr to import from `/downloads` once a torrent reports completed.

### 3. Path matching (the thing that trips everyone up)

Sonarr/Radarr import by looking at the path the download client reports. This container reports `/<downloads>/<category>/…`.

- **If Sonarr/Radarr mount the same share at the same path as this container** (e.g. everything uses `/data` or everything uses `/downloads`) → nothing to do. ✅
- **If their internal path differs** (say Sonarr sees `/data/downloads` but this container writes to `/downloads`), you have two options:
  - Set **`SAVE_PATH=/data/downloads`** on this container so it reports the path Sonarr expects, **or**
  - Add a **Remote Path Mapping** in Sonarr/Radarr (**Settings → Download Clients → Remote Path Mappings**): Host = the client Host you entered, Remote Path = `/downloads/`, Local Path = `/data/downloads/`.

Recommended clean setup (mirrors TRaSH guides): mount your one media share at **`/data`** in Sonarr, Radarr **and** this container, use categories `tv-sonarr` / `radarr`, and point the Downloads path here at `/data/downloads`.

### 4. Test the whole flow

Search for an episode/movie and hit download. In the *arr **Activity/Queue** you'll see it progress: it climbs while TorBox downloads in the cloud, sits near ~90% while the files are pulled locally, then flips to **completed** and imports into your library.

---

## Web UI

Open `http://<host>:8080/` in a browser for a small dashboard. It refreshes automatically every few seconds and has four tabs:

- **Activity** — everything currently tracked, with separate progress bars for the cloud phase (TorBox downloading) and the local pull to `/downloads`, plus speeds, sizes and errors. The header also shows how many days are left on your **TorBox subscription** (colored amber inside the warning window, red when ≤ 3 days or expired).
- **History** — a persistent event log of what happened: **Added** (sent to TorBox), **Downloaded** (files landed in `/downloads`), **Transferred** (imported and removed by Sonarr/Radarr), **Cloud cleaned** (cloud copies removed by cleanup), and any **Errors**. Kept in SQLite (last 1000 events), so it survives restarts and shows items after the *arr apps delete them.
- **Logs** — a live debug log (filterable by level) capturing the worker, TorBox API calls, and every request Sonarr/Radarr make. In-memory, last 2000 lines; `docker logs` still honours `LOG_LEVEL`.
- **Settings** — runtime-tunable settings, applied immediately (no restart) and persisted in `/data` (they override the matching environment variables):
  - **Download speed limit** (MiB/s) — live cap on the aggregate pull speed from TorBox, even mid-download.
  - **Auto-clear items older than N days** — every 30 minutes, anything in your TorBox account older than the cutoff is deleted (including items you added outside this app); anything still being pulled locally is spared.
  - **Delete cloud copy after import** (hours) — same as `TORBOX_CLEANUP_HOURS`.
  - **Subscription warning** — days-before-expiry threshold for the header badge and notifications.
  - **Pushover notifications** — enter your Pushover app token + user key (pushover.net) and use *Send test notification* to verify. You then get: a subscription-expiry warning (at most **once per day**, high priority when ≤ 2 days), and an **error-burst alert** — one message when a configurable number of errors (default 25) pile up within 15 minutes, followed by an hour of silence. Repeated failed TorBox polls count too, so a dead API key or outage triggers exactly one alert instead of a spam feed — or silence.

Log in with the same `QBIT_USER` / `QBIT_PASS` credentials Sonarr/Radarr use. Sessions are in-memory, so you'll be asked to sign in again after the container restarts.

---

## Configuration reference

All configuration is via environment variables (see `.env.example`):

| Variable | Default | Description |
|---|---|---|
| `TORBOX_API_KEY` | — | **Required.** Your TorBox API key. |
| `QBIT_USER` / `QBIT_PASS` | `admin` / `adminadmin` | Credentials Sonarr/Radarr log in with. |
| `DOWNLOAD_DIR` | `/downloads` | Where this container writes finished files. |
| `SAVE_PATH` | = `DOWNLOAD_DIR` | Path reported to *arr (set only if their mount path differs). |
| `PUID` / `PGID` | `1000` / `1000` | File ownership. **Use `99` / `100` on Unraid.** |
| `UMASK` | `022` | Umask for created files. |
| `POLL_INTERVAL` | `15` | Seconds between TorBox status polls. |
| `MAX_PARALLEL_DOWNLOADS` | `4` | Concurrent file downloads from the TorBox CDN. |
| `MAX_PARALLEL_TORRENTS` | `2` | Torrents pulled locally at the same time (0 = unlimited). |
| `MAX_DOWNLOAD_SPEED` | `0` | Aggregate download cap in MiB/s across all files (0 = unlimited). |
| `STALL_TIMEOUT` | `90` | Seconds without data before a stalled stream is retried with a fresh link. |
| `DOWNLOAD_RETRIES` | `4` | Attempts per file; each retry resumes from the bytes already on disk. |
| `TORBOX_CLEANUP_HOURS` | `24` | Delete the TorBox **cloud** copy this long after the local download completes, freeing your TorBox active-torrent slots (local files kept; 0 = never). ✏️ |
| `CLOUD_MAX_AGE_DAYS` | `0` | Delete **any** item in the TorBox account older than this many days, tracked or not (0 = off). Items still being pulled locally are spared. ✏️ |
| `DELETE_FROM_TORBOX_ON_REMOVE` | `true` | Delete the cloud torrent when *arr removes the download. |
| `TORBOX_SEED` | `1` | TorBox seeding: 1=auto, 2=always, 3=never. |
| `SUB_WARN_DAYS` | `7` | Warn when the TorBox subscription has this many days left (0 = off). ✏️ |
| `ERROR_BURST_THRESHOLD` | `25` | Pushover alert after this many errors within 15 minutes (0 = off). ✏️ |
| `PUSHOVER_TOKEN` / `PUSHOVER_USER` | — | Pushover application token + user key. ✏️ |
| `PUSHOVER_ENABLED` | `true` | Master switch for Pushover (only matters once token+user are set). ✏️ |
| `LOG_LEVEL` | `INFO` | `DEBUG` for verbose logging. |

Variables marked ✏️ are only **defaults**: they can be changed at runtime in the web UI's **Settings** tab, and a value saved there (stored in `/data`) overrides the environment variable from then on.

## Notes & limitations

- **Torrents only.** Usenet (SABnzbd emulation) is not implemented.
- **Imports fail with "file not found"?** Your paths don't match — see *Path matching* above.
- State is kept in SQLite at `/data/state.db`; keep that volume to survive restarts.

## Development

```bash
pip install -r requirements.txt
TORBOX_API_KEY=... DOWNLOAD_DIR=./dl DATA_DIR=./data \
  uvicorn app.main:app --reload --port 8080
```

Run the tests:

```bash
pip install -r requirements-dev.txt
pytest
```

CI runs the suite on every push and pull request; the Docker image only publishes after tests pass.

Layout:

```
app/
  main.py           FastAPI app + lifespan wiring, request tracing, /health
  config.py         env-based settings (defaults)
  runtime.py        runtime-tunable settings (web UI Settings tab, persisted in SQLite)
  qbit_api.py       qBittorrent Web API v2 emulation (talks to Sonarr/Radarr)
  torbox_client.py  async TorBox v1 API client
  worker.py         poll TorBox + download finished files locally + housekeeping
                    (subscription status, age-based cloud cleanup)
  notify.py         Pushover notifications (subscription expiry, error bursts)
  store.py          SQLite state
  bencode.py        infohash computation (magnet + .torrent)
  webui.py          dashboard endpoints (serves /, state/history/logs/settings JSON)
  logbuffer.py      in-memory ring buffer + secret redaction for the Logs tab
  static/index.html the dashboard page (vanilla HTML/JS, no build step)
tests/                pytest suite (bencode, config, store, worker, qbit_api, logbuffer,
                      runtime, notify, webui)
docker-entrypoint.sh  PUID/PGID privilege drop
templates/torbox-client.xml  Unraid Community Applications template
.github/workflows/    tests, then builds & publishes the image to GHCR
```
