"""Runtime configuration, sourced entirely from environment variables.

Everything that a user might want to tweak lives here so the rest of the code
can import a single ``settings`` object.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _str(name: str, default: str) -> str:
    """Env string where empty/whitespace counts as unset.

    Unraid templates and ``VAR=`` lines in .env files set variables to "",
    which must not override defaults (an empty SAVE_PATH would make every
    path we report to Sonarr/Radarr relative).
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # --- TorBox ---
    torbox_api_key: str = _str("TORBOX_API_KEY", "")
    torbox_base_url: str = _str("TORBOX_BASE_URL", "https://api.torbox.app/v1")

    # --- qBittorrent emulation / auth ---
    # Sonarr/Radarr log in with these. Defaults match qBittorrent's own defaults.
    qbit_user: str = _str("QBIT_USER", "admin")
    qbit_pass: str = _str("QBIT_PASS", "adminadmin")
    # Version strings we advertise to the *arr apps.
    qbit_app_version: str = _str("QBIT_APP_VERSION", "v4.6.5")
    qbit_webapi_version: str = _str("QBIT_WEBAPI_VERSION", "2.9.3")

    # --- Storage ---
    # Where THIS container writes finished files (its own mount point).
    download_dir: str = _str("DOWNLOAD_DIR", "/downloads")
    # The base path the *arr containers see for the same files. Usually identical
    # to download_dir when both mount the same host directory at /downloads.
    save_path_base: str = _str("SAVE_PATH", _str("DOWNLOAD_DIR", "/downloads"))
    # SQLite state file.
    data_dir: str = _str("DATA_DIR", "/data")

    # --- Behaviour ---
    poll_interval: int = _int("POLL_INTERVAL", 15)  # seconds between TorBox polls
    max_parallel_downloads: int = _int("MAX_PARALLEL_DOWNLOADS", 4)  # concurrent file downloads
    delete_from_torbox_on_remove: bool = _bool("DELETE_FROM_TORBOX_ON_REMOVE", True)
    # If TorBox seeding is requested (1=auto, 2=always seed, 3=never seed).
    torbox_seed: int = _int("TORBOX_SEED", 1)
    # Allow TorBox to zip download folders (we download per-file, so keep off).
    torbox_allow_zip: bool = _bool("TORBOX_ALLOW_ZIP", False)

    listen_host: str = _str("LISTEN_HOST", "0.0.0.0")
    listen_port: int = _int("LISTEN_PORT", 8080)
    log_level: str = _str("LOG_LEVEL", "INFO").upper()

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "state.db")


settings = Settings()
