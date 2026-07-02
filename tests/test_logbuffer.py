import logging

from app.logbuffer import RedactSecrets


def _record(msg, *args):
    return logging.LogRecord("t", logging.INFO, __file__, 1, msg, args, None)


def test_redacts_known_secret_value():
    f = RedactSecrets("supersecretkey")
    rec = _record("using key supersecretkey now")
    f.filter(rec)
    assert "supersecretkey" not in rec.getMessage()
    assert "***" in rec.getMessage()


def test_redacts_token_query_param():
    f = RedactSecrets()
    rec = _record("HTTP Request: GET https://api/requestdl?token=abc123DEF&id=9")
    f.filter(rec)
    out = rec.getMessage()
    assert "abc123DEF" not in out
    assert "token=***" in out
    assert "id=9" in out  # non-secret params preserved


def test_redacts_bearer():
    f = RedactSecrets()
    rec = _record("Authorization: Bearer abc.def.ghi")
    f.filter(rec)
    assert "abc.def.ghi" not in rec.getMessage()


def test_redacts_when_secret_in_args():
    f = RedactSecrets("hunter2")
    rec = _record("password is %s", "hunter2")
    f.filter(rec)
    assert "hunter2" not in rec.getMessage()


def test_passthrough_when_nothing_to_redact():
    f = RedactSecrets("secret")
    rec = _record("nothing sensitive here")
    assert f.filter(rec) is True
    assert rec.getMessage() == "nothing sensitive here"
