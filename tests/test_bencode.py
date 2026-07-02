import hashlib

import pytest

from app import bencode


def test_roundtrip_types():
    value = {b"a": 1, b"b": [b"x", b"y"], b"c": {b"n": 42}}
    assert bencode.decode(bencode.encode(value)) == value


def test_encode_sorts_dict_keys():
    # Bencode requires dict keys in sorted order; the info-hash depends on it.
    assert bencode.encode({b"b": 1, b"a": 2}) == b"d1:ai2e1:bi1ee"


def test_encode_rejects_bool():
    with pytest.raises(TypeError):
        bencode.encode(True)


def test_infohash_from_torrent_matches_manual_sha1():
    info = {b"name": b"file.mkv", b"length": 5, b"piece length": 262144}
    meta = {b"info": info, b"announce": b"http://tracker"}
    data = bencode.encode(meta)
    expected = hashlib.sha1(bencode.encode(info)).hexdigest()
    assert bencode.infohash_from_torrent(data) == expected


def test_infohash_from_magnet_hex_and_base32():
    forty = "a" * 40
    assert bencode.infohash_from_magnet(f"magnet:?xt=urn:btih:{forty}&dn=x") == forty
    # base32 form decodes to the same 40-hex value
    b32 = "MFRGGZDFMZTWQ2LKNNWG23TPOA5A"[:32].ljust(32, "A")
    out = bencode.infohash_from_magnet(f"magnet:?xt=urn:btih:{b32}")
    assert out is not None and len(out) == 40


def test_infohash_from_magnet_none_when_missing():
    assert bencode.infohash_from_magnet("magnet:?dn=noxt") is None


def test_decode_rejects_deep_nesting():
    # A hostile torrent must not blow the Python stack.
    bomb = b"l" * 5000 + b"e" * 5000
    with pytest.raises(ValueError):
        bencode.decode(bomb)


def test_torrent_name_survives_bad_input():
    assert bencode.torrent_name(b"not a torrent") is None
