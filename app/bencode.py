"""Minimal bencode decoder/encoder plus infohash helpers.

We need the *real* v1 infohash for every torrent we hand to Sonarr/Radarr,
because that hash is how they match a grabbed release to a queue item. For
magnets we parse the ``btih`` from the URI; for ``.torrent`` files we hash the
bencoded ``info`` dictionary ourselves.
"""
from __future__ import annotations

import base64
import hashlib
from typing import Any, Tuple
from urllib.parse import parse_qs, urlparse


def _decode(data: bytes, index: int) -> Tuple[Any, int]:
    ch = data[index:index + 1]
    if ch == b"i":
        end = data.index(b"e", index)
        return int(data[index + 1:end]), end + 1
    if ch == b"l":
        index += 1
        result = []
        while data[index:index + 1] != b"e":
            item, index = _decode(data, index)
            result.append(item)
        return result, index + 1
    if ch == b"d":
        index += 1
        result = {}
        while data[index:index + 1] != b"e":
            key, index = _decode(data, index)
            value, index = _decode(data, index)
            result[key] = value
        return result, index + 1
    if ch.isdigit():
        colon = data.index(b":", index)
        length = int(data[index:colon])
        start = colon + 1
        return data[start:start + length], start + length
    raise ValueError(f"Invalid bencode at index {index}: {ch!r}")


def decode(data: bytes) -> Any:
    value, _ = _decode(data, 0)
    return value


def encode(value: Any) -> bytes:
    if isinstance(value, bool):
        raise TypeError("bool is not bencodable")
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, str):
        raw = value.encode()
        return str(len(raw)).encode() + b":" + raw
    if isinstance(value, list):
        return b"l" + b"".join(encode(v) for v in value) + b"e"
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda kv: kv[0])
        return b"d" + b"".join(encode(k) + encode(v) for k, v in items) + b"e"
    raise TypeError(f"Cannot bencode {type(value)}")


def infohash_from_torrent(data: bytes) -> str:
    """SHA-1 of the bencoded ``info`` dict → lowercase hex (BitTorrent v1)."""
    meta = decode(data)
    info = meta[b"info"]
    return hashlib.sha1(encode(info)).hexdigest().lower()


def infohash_from_magnet(magnet: str) -> str | None:
    """Extract the v1 btih from a magnet URI as lowercase hex, or ``None``."""
    query = parse_qs(urlparse(magnet).query)
    for xt in query.get("xt", []):
        if not xt.lower().startswith("urn:btih:"):
            continue
        value = xt.split(":", 2)[2]
        if len(value) == 40:
            return value.lower()
        if len(value) == 32:  # base32 encoded
            try:
                return base64.b32decode(value.upper()).hex().lower()
            except Exception:
                return None
    return None


def torrent_name(data: bytes) -> str | None:
    try:
        info = decode(data)[b"info"]
        name = info.get(b"name")
        if isinstance(name, bytes):
            return name.decode("utf-8", "replace")
    except Exception:
        return None
    return None
