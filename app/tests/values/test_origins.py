import pytest

from values.names import normalize_origins


@pytest.mark.parametrize("value, expected", [
    (None, None), ("", None), ([], None), (["", "  "], None),
    (["https://app.example.test"], ["https://app.example.test"]),
    ([" HTTPS://APP.example.test:443/ ", "https://app.example.test"], ["https://app.example.test"]),
    (["http://localhost:5173"], ["http://localhost:5173"]),
    (["http://127.0.0.1:3000"], ["http://127.0.0.1:3000"]),
    (["https://[::1]:8443"], ["https://[::1]:8443"]),
    (["*"], ["*"]), (["null"], ["null"]),
    (["https://app.example.test", "http://localhost:5173"], ["https://app.example.test", "http://localhost:5173"]),
])
def test_valid_origin_lists_are_canonical_and_blank_remains_blank(value, expected):
  assert normalize_origins(value) == expected


@pytest.mark.parametrize("value", ["https://app.example.test", {}, 5, True, [None], [2], [False]])
def test_origins_need_a_list_of_strings(value):
  with pytest.raises(ValueError, match="Allowed origins must"):
    normalize_origins(value)


@pytest.mark.parametrize("origin", [
    "app.example.test", "https://", "ftp://app.example.test", "wss://app.example.test",
    "https://app.example.test/path", "https://app.example.test?", "https://app.example.test?q=x",
    "https://app.example.test#", "https://app.example.test#anchor", "https://user:pass@app.example.test",
    "https://@app.example.test", "https://*.example.test", "https://app.example.test:bad",
    "https://app.example.test:65536", "https://app.example.test\\path", "https://app.exa mple.test",
    "https://app.example.test\r\nAccess-Control-Allow-Origin: *", "https://app.ex\tample.test",
    "https://app.example.test\x00", "https://app.example.test\x7f",
])
def test_invalid_origins_are_refused(origin):
  with pytest.raises(ValueError, match="Invalid origin"):
    normalize_origins([origin])
