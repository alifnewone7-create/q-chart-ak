"""New unit tests (iteration 2) for the rewritten Login token-extraction.

Covers scenarios described in the review request:
(a) POST-response reuse: Login.response already points to a /trade URL with
    window.settings — get_profile must extract token WITHOUT calling
    send_request at all.
(b) Token-anywhere regex fallback: HTML has no window.settings but contains
    "token":"<20 alnum>" in a script -> get_profile returns {"token": ...}.
(c) Undecodable garbage/binary content (simulating undecoded brotli):
    get_profile returns (None, None) gracefully AND writes login_debug.html
    (cleaned up after test).
(d) Minified window.settings={...}; with no spaces still extracted (already
    covered by test_a2_no_spaces_around_equals but re-checked in POST-reuse
    context here).
(e) Login object with NO .response attribute at all must not raise
    AttributeError (get_profile uses getattr guard).
"""
import re
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, "/app/backend")

from pyquotex.http.login import Login  # noqa: E402


# ---------- helpers ----------
class _Soup:
    """Minimal BeautifulSoup-like object over an HTML string."""

    def __init__(self, html):
        self.html = html

    def find_all(self, tag):
        return [
            _Script(t) for t in re.findall(
                r"<script[^>]*>(.*?)</script>", self.html, flags=re.DOTALL
            )
        ]


class _Script:
    def __init__(self, text):
        self._t = text

    def get_text(self):
        return self._t


def _bare_login():
    """Login instance with only the attrs required by _extract_settings /
    get_profile. send_request is a MagicMock so we can assert call_count."""
    lg = Login.__new__(Login)
    lg.api = MagicMock()
    lg.api.session_data = {}
    lg.api.username = "u@example.com"
    lg.headers = {"User-Agent": "ua"}
    lg.full_url = "https://qxbroker.com/en"
    lg.send_request = MagicMock()
    lg.get_cookies = lambda: {"c": "1"}
    lg._html = ""
    lg.get_soup = lambda: _Soup(lg._html)
    return lg


def _resp(text, url="https://qxbroker.com/en/trade", content=None):
    r = MagicMock()
    r.text = text
    r.url = url
    if content is not None:
        r.content = content
    return r


# =====================================================================
# (a) POST-response reuse
# =====================================================================
class TestPostResponseReuse:
    def test_a_post_response_reused_no_extra_get(self, monkeypatch):
        """Login.response already set to a /trade response with settings ->
        get_profile MUST NOT call send_request again."""
        monkeypatch.setattr(time, "sleep", lambda *_: None)
        with patch("pyquotex.http.login.update_session"):
            html = (
                "<html><body>"
                '<script>window.settings = {"token":"REUSED_TOKEN_123","x":1};</script>'
                "</body></html>"
            )
            lg = _bare_login()
            lg.response = _resp(html, url="https://qxbroker.com/en/trade")
            lg._html = html

            resp, settings = lg.get_profile()

            assert settings is not None
            assert settings["token"] == "REUSED_TOKEN_123"
            assert lg.ssid == "REUSED_TOKEN_123"
            assert lg.send_request.call_count == 0, (
                "send_request must NOT be called when POST response already "
                f"has settings, got {lg.send_request.call_count} calls"
            )

    def test_a_minified_no_spaces_in_post_response(self, monkeypatch):
        """Minified 'window.settings={...};' inside POST response reused."""
        monkeypatch.setattr(time, "sleep", lambda *_: None)
        with patch("pyquotex.http.login.update_session"):
            html = (
                "<html><body>"
                '<script>window.settings={"token":"MINIFIED_ABC","a":2};</script>'
                "</body></html>"
            )
            lg = _bare_login()
            lg.response = _resp(html)
            lg._html = html

            resp, settings = lg.get_profile()
            assert settings["token"] == "MINIFIED_ABC"
            assert lg.send_request.call_count == 0


# =====================================================================
# (b) token-anywhere fallback regex
# =====================================================================
class TestTokenAnywhereFallback:
    def test_b_token_regex_anywhere_no_window_settings(self, monkeypatch):
        """HTML has no window.settings but does contain '"token":"..."' -
        the final regex fallback in _extract_settings must catch it."""
        monkeypatch.setattr(time, "sleep", lambda *_: None)
        with patch("pyquotex.http.login.update_session"):
            html = (
                "<html><body>"
                '<script>var config = {"user":{"id":1},"token":"ABCDEFGHIJKLMNOPQRST","other":true};</script>'
                "</body></html>"
            )
            lg = _bare_login()
            lg.response = _resp(html)
            lg._html = html

            resp, settings = lg.get_profile()
            assert settings is not None
            assert settings["token"] == "ABCDEFGHIJKLMNOPQRST"
            assert lg.ssid == "ABCDEFGHIJKLMNOPQRST"


# =====================================================================
# (c) undecodable / garbage content
# =====================================================================
class TestGarbageContent:
    def test_c_undecodable_bytes_returns_none_and_writes_debug_file(
        self, monkeypatch, tmp_path
    ):
        """Simulate un-decoded brotli payload — .text may be missing or
        binary garbage. get_profile must return (None, None) gracefully AND
        write login_debug.html for the user."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(time, "sleep", lambda *_: None)
        with patch("pyquotex.http.login.update_session"):
            # Non-string .text triggers the content.decode fallback.
            binary = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\xff\xff" + b"\xaa" * 200
            r = MagicMock()
            r.text = None            # non-string -> triggers decode path
            r.url = "https://qxbroker.com/en/trade"
            r.content = binary

            lg = _bare_login()
            lg.response = r
            # Also make retried GETs return the same garbage
            lg.send_request = MagicMock(return_value=r)
            lg._html = ""

            resp, settings = lg.get_profile()
            assert resp is None
            assert settings is None

            # Debug file must have been written
            debug = Path("login_debug.html")
            assert debug.exists(), "login_debug.html must be written on failure"
            # File must be a string (decoded with errors='replace')
            content = debug.read_text(encoding="utf-8", errors="replace")
            assert isinstance(content, str) and len(content) > 0

            # Cleanup handled by monkeypatch.chdir(tmp_path); still remove
            # in case a previous test wrote it in cwd.
            try:
                debug.unlink()
            except FileNotFoundError:
                pass


# =====================================================================
# (e) missing .response attribute
# =====================================================================
class TestNoResponseAttr:
    def test_e_no_response_attr_does_not_raise(self, monkeypatch):
        """Login object with NO .response attribute -> getattr guard must
        prevent AttributeError."""
        monkeypatch.setattr(time, "sleep", lambda *_: None)
        with patch("pyquotex.http.login.update_session"):
            lg = _bare_login()
            # Explicitly delete .response if present
            if hasattr(lg, "response"):
                delattr(lg, "response")

            # Make retry GETs return HTML with a token so the flow completes
            html = (
                "<html><body>"
                '<script>window.settings = {"token":"AFTER_RETRY_TOKEN"};</script>'
                "</body></html>"
            )
            r = _resp(html)

            def _sr(method=None, url=None, data=None):
                lg._html = html
                return r

            lg.send_request = MagicMock(side_effect=_sr)

            try:
                resp, settings = lg.get_profile()
            except AttributeError as e:
                pytest.fail(f"get_profile must not raise AttributeError: {e}")

            assert settings is not None
            assert settings["token"] == "AFTER_RETRY_TOKEN"


# =====================================================================
# Edge cases (extra coverage per main-agent hint)
# =====================================================================
class TestEdgeCases:
    def test_response_url_non_string_object(self, monkeypatch):
        """response.url is a non-string object (e.g. yarl.URL/bytes). The
        'trade' in str(...) check must tolerate this without raising."""
        monkeypatch.setattr(time, "sleep", lambda *_: None)
        with patch("pyquotex.http.login.update_session"):

            class WeirdURL:
                def __str__(self):
                    return "https://qxbroker.com/en/trade?x=1"

            html = (
                "<html><body>"
                '<script>window.settings = {"token":"WEIRD_URL_OK"};</script>'
                "</body></html>"
            )
            lg = _bare_login()
            r = _resp(html, url=WeirdURL())
            lg.response = r
            lg._html = html

            resp, settings = lg.get_profile()
            assert settings["token"] == "WEIRD_URL_OK"
            assert lg.send_request.call_count == 0

    def test_response_url_not_trade_falls_through_to_get(self, monkeypatch):
        """If POST response URL is NOT /trade (e.g. still on /sign-in), the
        POST reuse path is skipped and send_request GET /trade is issued."""
        monkeypatch.setattr(time, "sleep", lambda *_: None)
        with patch("pyquotex.http.login.update_session"):
            html_ok = (
                "<html><body>"
                '<script>window.settings = {"token":"FROM_GET"};</script>'
                "</body></html>"
            )
            lg = _bare_login()
            # POST response points to sign-in page - no reuse
            lg.response = _resp("<html>no settings</html>",
                                url="https://qxbroker.com/en/sign-in")
            lg._html = ""

            def _sr(method=None, url=None, data=None):
                lg._html = html_ok
                return _resp(html_ok)

            lg.send_request = MagicMock(side_effect=_sr)

            resp, settings = lg.get_profile()
            assert settings["token"] == "FROM_GET"
            assert lg.send_request.call_count == 1, (
                "One GET /trade should have been issued"
            )

    def test_debug_file_write_failure_is_swallowed(self, monkeypatch, tmp_path):
        """If writing login_debug.html itself fails, get_profile must still
        return (None, None) without raising."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(time, "sleep", lambda *_: None)

        original_write_text = Path.write_text

        def boom(self, *a, **kw):
            if self.name == "login_debug.html":
                raise OSError("disk full")
            return original_write_text(self, *a, **kw)

        monkeypatch.setattr(Path, "write_text", boom)

        with patch("pyquotex.http.login.update_session"):
            html = "<html><body>nope</body></html>"
            lg = _bare_login()
            r = _resp(html)
            lg.response = r
            lg.send_request = MagicMock(return_value=r)
            lg._html = html

            try:
                resp, settings = lg.get_profile()
            except Exception as e:
                pytest.fail(
                    f"get_profile must swallow debug-write errors, got: {e}"
                )
            assert (resp, settings) == (None, None)


# Auto-cleanup any login_debug.html accidentally created in project root
@pytest.fixture(autouse=True)
def _cleanup_debug_file():
    yield
    for p in (Path("/app/backend/login_debug.html"),
              Path("/app/login_debug.html"),
              Path.cwd() / "login_debug.html"):
        try:
            if p.exists():
                p.unlink()
        except Exception:
            pass
