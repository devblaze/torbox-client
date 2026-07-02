from app import config


def test_str_treats_empty_and_whitespace_as_unset(monkeypatch):
    monkeypatch.setenv("X_TEST", "")
    assert config._str("X_TEST", "fallback") == "fallback"
    monkeypatch.setenv("X_TEST", "   ")
    assert config._str("X_TEST", "fallback") == "fallback"
    monkeypatch.setenv("X_TEST", "  value  ")
    assert config._str("X_TEST", "fallback") == "value"


def test_str_unset_uses_default(monkeypatch):
    monkeypatch.delenv("X_TEST", raising=False)
    assert config._str("X_TEST", "fallback") == "fallback"


def test_int_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("X_INT", "notanint")
    assert config._int("X_INT", 7) == 7
    monkeypatch.setenv("X_INT", "12")
    assert config._int("X_INT", 7) == 12
    monkeypatch.setenv("X_INT", "")
    assert config._int("X_INT", 7) == 7


def test_float_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("X_F", "abc")
    assert config._float("X_F", 1.5) == 1.5
    monkeypatch.setenv("X_F", "2.5")
    assert config._float("X_F", 1.5) == 2.5


def test_bool_parsing(monkeypatch):
    for truthy in ("1", "true", "YES", "on"):
        monkeypatch.setenv("X_B", truthy)
        assert config._bool("X_B", False) is True
    for falsy in ("0", "false", "no", "off", "garbage"):
        monkeypatch.setenv("X_B", falsy)
        assert config._bool("X_B", True) is False
    monkeypatch.delenv("X_B", raising=False)
    assert config._bool("X_B", True) is True


def test_empty_save_path_falls_back_to_download_dir(monkeypatch):
    # The bug that broke Sonarr/Radarr imports: SAVE_PATH="" must not win.
    monkeypatch.setenv("SAVE_PATH", "")
    monkeypatch.setenv("DOWNLOAD_DIR", "/downloads")
    assert config._str("SAVE_PATH", config._str("DOWNLOAD_DIR", "/downloads")) == "/downloads"
