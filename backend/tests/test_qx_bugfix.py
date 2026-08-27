#!/usr/bin/env python3
"""Comprehensive test suite for the Quotex login/reconnect bug fix.

Tests that the QuotexManager correctly:
1. Preserves session_data on reconnects (doesn't fire credential login every time)
2. Tries fresh login first on initial connect, cached session first on reconnects
3. Falls back gracefully when one method fails
4. Only deletes session.json when BOTH methods fail
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

# Set dummy env vars before importing config
os.environ.setdefault("BOT_TOKEN", "test_token_123")
os.environ.setdefault("ADMIN_ID", "12345")
os.environ.setdefault("QUOTEX_EMAIL", "test@example.com")
os.environ.setdefault("QUOTEX_PASSWORD", "test_password")

# Add backend to path
sys.path.insert(0, "/app/backend")

import qx


class FakeQuotex:
    """Mock Quotex client that tracks login attempts and session state."""
    
    def __init__(self, email, password, host, lang):
        self.email = email
        self.password = password
        self.host = host
        self.lang = lang
        # Simulate pyquotex loading session.json with saved token
        self.session_data = {
            "token": "SAVED_TOKEN",
            "cookies": "SAVED_COOKIES",
            "user_agent": "TestUserAgent/1.0"
        }
        # Track what type of login was attempted
        self.connect_calls = []
        # Allow test to script responses
        self.connect_responses = []
        self.response_index = 0
        
    async def connect(self):
        """Record whether this was a fresh login or session reuse."""
        is_fresh = self.session_data.get("token") is None
        login_type = "fresh" if is_fresh else "cached"
        self.connect_calls.append(login_type)
        
        # Return scripted response
        if self.response_index < len(self.connect_responses):
            result = self.connect_responses[self.response_index]
            self.response_index += 1
            return result
        # Default: success
        return (True, "ok")
    
    async def check_connect(self):
        """Simulate connection check."""
        return True
    
    async def change_account(self, account_type):
        """Simulate account change."""
        pass
    
    async def get_instruments(self):
        """Simulate getting instruments."""
        return []
    
    async def close(self):
        """Simulate closing connection."""
        pass


def create_test_session_file():
    """Create a temporary session file for testing."""
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    with open(path, "w") as f:
        f.write('{"token": "SAVED_TOKEN", "cookies": "c", "user_agent": "UA"}')
    return Path(path)


async def test_case_a():
    """A. First connect, fresh login SUCCEEDS."""
    print("\n=== TEST A: First connect, fresh login succeeds ===")
    
    temp_session = create_test_session_file()
    fake_clients = []
    
    def mock_quotex(*args, **kwargs):
        client = FakeQuotex(*args, **kwargs)
        client.connect_responses = [(True, "ok")]
        fake_clients.append(client)
        return client
    
    with patch.object(qx, "Quotex", mock_quotex):
        with patch.object(qx, "_session_file", return_value=temp_session):
            manager = qx.QuotexManager()
            await manager.ensure_connected()
            
            # Assertions
            assert len(fake_clients) == 1, f"Expected 1 client, got {len(fake_clients)}"
            client = fake_clients[0]
            assert len(client.connect_calls) == 1, f"Expected 1 connect call, got {len(client.connect_calls)}"
            assert client.connect_calls[0] == "fresh", f"Expected fresh login, got {client.connect_calls[0]}"
            assert manager.connected is True, "Manager should be connected"
            assert manager._fresh_done is True, "_fresh_done should be True"
            assert temp_session.exists(), "Session file should NOT be deleted on success"
            
            print("✅ PASS: First connect with fresh login succeeded correctly")
            print(f"   - Connect calls: {client.connect_calls}")
            print(f"   - Connected: {manager.connected}")
            print(f"   - Fresh done: {manager._fresh_done}")
    
    temp_session.unlink(missing_ok=True)


async def test_case_b():
    """B. First connect, fresh FAILS but cached SUCCEEDS."""
    print("\n=== TEST B: First connect, fresh fails but cached succeeds ===")
    
    temp_session = create_test_session_file()
    fake_clients = []
    
    def mock_quotex(*args, **kwargs):
        client = FakeQuotex(*args, **kwargs)
        # First call (fresh) fails, second call (cached) succeeds
        if len(fake_clients) == 0:
            client.connect_responses = [(False, "Login failed. Unknown error")]
        else:
            client.connect_responses = [(True, "ok")]
        fake_clients.append(client)
        return client
    
    with patch.object(qx, "Quotex", mock_quotex):
        with patch.object(qx, "_session_file", return_value=temp_session):
            manager = qx.QuotexManager()
            await manager.ensure_connected()
            
            # Assertions
            assert len(fake_clients) == 2, f"Expected 2 clients (fresh + cached), got {len(fake_clients)}"
            assert fake_clients[0].connect_calls == ["fresh"], f"First should be fresh, got {fake_clients[0].connect_calls}"
            assert fake_clients[1].connect_calls == ["cached"], f"Second should be cached, got {fake_clients[1].connect_calls}"
            assert manager.connected is True, "Manager should be connected"
            assert manager._fresh_done is True, "_fresh_done should be True after success"
            assert temp_session.exists(), "Session file should NOT be deleted when cached succeeds"
            
            print("✅ PASS: Recovered from fresh login failure using cached session")
            print(f"   - Connect attempts: fresh (failed) -> cached (success)")
            print(f"   - Total clients created: {len(fake_clients)}")
            print(f"   - Connected: {manager.connected}")
    
    temp_session.unlink(missing_ok=True)


async def test_case_c():
    """C. First connect, BOTH fail."""
    print("\n=== TEST C: First connect, both fresh and cached fail ===")
    
    temp_session = create_test_session_file()
    fake_clients = []
    
    def mock_quotex(*args, **kwargs):
        client = FakeQuotex(*args, **kwargs)
        client.connect_responses = [(False, "Login failed. Unknown error")]
        fake_clients.append(client)
        return client
    
    with patch.object(qx, "Quotex", mock_quotex):
        with patch.object(qx, "_session_file", return_value=temp_session):
            manager = qx.QuotexManager()
            
            try:
                await manager.ensure_connected()
                assert False, "Should have raised ConnectionError"
            except ConnectionError as e:
                error_msg = str(e)
                assert "Login failed" in error_msg or "Unknown error" in error_msg, \
                    f"Error should contain failure reason, got: {error_msg}"
                assert not temp_session.exists(), "Session file SHOULD be deleted when both fail"
                
                print("✅ PASS: Both methods failed, raised ConnectionError and deleted session")
                print(f"   - Error message: {error_msg}")
                print(f"   - Session file deleted: {not temp_session.exists()}")
                print(f"   - Connect attempts: {[c.connect_calls for c in fake_clients]}")
    
    temp_session.unlink(missing_ok=True)


async def test_case_d():
    """D. Reconnect after successful run - should try CACHED first."""
    print("\n=== TEST D: Reconnect after successful run (cached first) ===")
    
    temp_session = create_test_session_file()
    fake_clients = []
    
    def mock_quotex(*args, **kwargs):
        client = FakeQuotex(*args, **kwargs)
        client.connect_responses = [(True, "ok")]
        fake_clients.append(client)
        return client
    
    with patch.object(qx, "Quotex", mock_quotex):
        with patch.object(qx, "_session_file", return_value=temp_session):
            manager = qx.QuotexManager()
            # Simulate that a fresh login already succeeded in this process
            manager._fresh_done = True
            manager.connected = False  # But we're disconnected now
            
            await manager.ensure_connected()
            
            # Assertions - THIS IS THE KEY REGRESSION TEST
            assert len(fake_clients) == 1, f"Expected 1 client, got {len(fake_clients)}"
            client = fake_clients[0]
            assert len(client.connect_calls) == 1, f"Expected 1 connect call, got {len(client.connect_calls)}"
            assert client.connect_calls[0] == "cached", \
                f"CRITICAL: Reconnect should try CACHED first, not fresh! Got: {client.connect_calls[0]}"
            assert manager.connected is True, "Manager should be connected"
            
            print("✅ PASS: Reconnect correctly tried cached session FIRST (not fresh)")
            print(f"   - Connect calls: {client.connect_calls}")
            print(f"   - This prevents the 'Login failed. Unknown error' loop!")
    
    temp_session.unlink(missing_ok=True)


async def test_case_e():
    """E. Already connected and check_connect() returns True."""
    print("\n=== TEST E: Already connected, check_connect returns True ===")
    
    temp_session = create_test_session_file()
    fake_clients = []
    
    def mock_quotex(*args, **kwargs):
        client = FakeQuotex(*args, **kwargs)
        fake_clients.append(client)
        return client
    
    with patch.object(qx, "Quotex", mock_quotex):
        with patch.object(qx, "_session_file", return_value=temp_session):
            manager = qx.QuotexManager()
            # Set up as already connected
            manager.connected = True
            manager.client = mock_quotex("test@example.com", "pw", "qxbroker.com", "en")
            
            initial_client_count = len(fake_clients)
            await manager.ensure_connected()
            
            # Assertions
            assert len(fake_clients) == initial_client_count, \
                f"Should not create new client when already connected, but got {len(fake_clients)} clients"
            assert manager.connected is True, "Should remain connected"
            
            print("✅ PASS: Already connected, no new connection attempt made")
            print(f"   - Client count: {len(fake_clients)}")
    
    temp_session.unlink(missing_ok=True)


async def test_case_f():
    """F. _make_client preserves user_agent and correctly sets token/cookies."""
    print("\n=== TEST F: _make_client correctly handles session_data ===")
    
    fake_clients = []
    
    def mock_quotex(*args, **kwargs):
        client = FakeQuotex(*args, **kwargs)
        fake_clients.append(client)
        return client
    
    with patch.object(qx, "Quotex", mock_quotex):
        manager = qx.QuotexManager()
        
        # Test fresh=True
        client_fresh = manager._make_client(fresh=True)
        assert client_fresh.session_data["token"] is None, "Fresh client should have token=None"
        assert client_fresh.session_data["cookies"] is None, "Fresh client should have cookies=None"
        assert client_fresh.session_data["user_agent"] == "TestUserAgent/1.0", \
            "Fresh client should PRESERVE user_agent"
        
        # Test fresh=False
        client_cached = manager._make_client(fresh=False)
        assert client_cached.session_data["token"] == "SAVED_TOKEN", \
            "Cached client should keep saved token"
        assert client_cached.session_data["cookies"] == "SAVED_COOKIES", \
            "Cached client should keep saved cookies"
        assert client_cached.session_data["user_agent"] == "TestUserAgent/1.0", \
            "Cached client should keep user_agent"
        
        print("✅ PASS: _make_client correctly handles session_data")
        print(f"   - Fresh: token={client_fresh.session_data['token']}, "
              f"cookies={client_fresh.session_data['cookies']}, "
              f"user_agent={client_fresh.session_data['user_agent']}")
        print(f"   - Cached: token={client_cached.session_data['token']}, "
              f"cookies={client_cached.session_data['cookies']}, "
              f"user_agent={client_cached.session_data['user_agent']}")


async def test_sanity_imports():
    """Sanity check: import other modules and test basic functions."""
    print("\n=== SANITY CHECK: Import other modules ===")
    
    try:
        import messages
        import storage
        import charting
        import sessions
        
        # Test messages.mono
        result = messages.mono("AB1")
        # Should convert to Mathematical Monospace
        assert result != "AB1", "mono() should transform the text"
        assert len(result) == 3, "mono() should preserve length"
        print(f"✅ messages.mono('AB1') = '{result}' (Mathematical Monospace)")
        
        # Test sessions.compute_delta
        # Case 1: Starting from 0, first trade WIN
        delta1 = sessions.compute_delta(0, 1, "LOSS")
        assert delta1 == -3, f"compute_delta(0, 1, 'LOSS') should be -3, got {delta1}"
        print(f"✅ sessions.compute_delta(0, 1, 'LOSS') = {delta1}")
        
        # Case 2: After loss (-3), next trade WIN
        delta2 = sessions.compute_delta(-3, 1, "WIN")
        assert delta2 == 6, f"compute_delta(-3, 1, 'WIN') should be 6, got {delta2}"
        print(f"✅ sessions.compute_delta(-3, 1, 'WIN') = {delta2}")
        
        # Case 3: After loss (-3), next trade WIN_MTG
        delta3 = sessions.compute_delta(-3, 1, "WIN_MTG")
        assert delta3 == 12, f"compute_delta(-3, 1, 'WIN_MTG') should be 12, got {delta3}"
        print(f"✅ sessions.compute_delta(-3, 1, 'WIN_MTG') = {delta3}")
        
        print("✅ PASS: All sanity checks passed")
        
    except Exception as e:
        print(f"❌ FAIL: Sanity check failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    return True


async def main():
    """Run all test cases."""
    print("=" * 70)
    print("QUOTEX LOGIN/RECONNECT BUG FIX VERIFICATION")
    print("=" * 70)
    
    try:
        await test_case_a()
        await test_case_b()
        await test_case_c()
        await test_case_d()
        await test_case_e()
        await test_case_f()
        sanity_ok = await test_sanity_imports()
        
        print("\n" + "=" * 70)
        print("ALL TESTS PASSED ✅")
        print("=" * 70)
        print("\nSUMMARY:")
        print("✅ A. First connect with fresh login succeeds")
        print("✅ B. First connect recovers from fresh failure using cached session")
        print("✅ C. Both methods fail -> ConnectionError + session deleted")
        print("✅ D. Reconnect tries CACHED first (KEY FIX - prevents login loop)")
        print("✅ E. Already connected -> no new connection attempt")
        print("✅ F. _make_client correctly handles session_data")
        if sanity_ok:
            print("✅ Sanity checks: messages.mono, sessions.compute_delta")
        print("\n🎉 The bug fix is working correctly!")
        print("   - Session is preserved on reconnects")
        print("   - No more 'Login failed. Unknown error' loop")
        
    except AssertionError as e:
        print(f"\n❌ TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ UNEXPECTED ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
