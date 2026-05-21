"""Tests for user authentication module — models, password hashing, AuthManager."""

import hashlib
import os

import pytest
from unittest.mock import MagicMock, patch

from user_authentication.models import (
    User,
    ROLE_SUPER_ADMIN,
    ROLE_ADMIN,
    ROLE_USER,
    ALL_ROLES,
    CAN_CREATE_ROLES,
)
from user_authentication.auth_manager import (
    _hash_password,
    _verify_password,
    _is_legacy_sha256,
    AuthManager,
)


def _legacy_sha256_hash(password: str, salt: str | None = None) -> str:
    """Recreate the pre-bcrypt SHA-256 hash format for backward-compat tests."""
    if salt is None:
        salt = os.urandom(16).hex()
    h = hashlib.sha256(f"{salt}{password}".encode()).hexdigest()
    return f"{salt}:{h}"


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------

class TestUser:
    def test_super_admin_permissions(self):
        u = User(email="sa@test.com", profile_name="SA", role=ROLE_SUPER_ADMIN)
        assert u.can_configure_clients is True
        assert u.can_manage_users is True
        assert set(u.creatable_roles) == {ROLE_SUPER_ADMIN, ROLE_ADMIN, ROLE_USER}

    def test_admin_permissions(self):
        u = User(email="a@test.com", profile_name="A", role=ROLE_ADMIN)
        assert u.can_configure_clients is True
        assert u.can_manage_users is True
        assert u.creatable_roles == [ROLE_USER]

    def test_user_permissions(self):
        u = User(email="u@test.com", profile_name="U", role=ROLE_USER)
        assert u.can_configure_clients is False
        assert u.can_manage_users is False
        assert u.creatable_roles == []

    def test_all_roles_defined(self):
        assert ROLE_SUPER_ADMIN in ALL_ROLES
        assert ROLE_ADMIN in ALL_ROLES
        assert ROLE_USER in ALL_ROLES

    def test_can_create_roles_hierarchy(self):
        assert ROLE_SUPER_ADMIN in CAN_CREATE_ROLES[ROLE_SUPER_ADMIN]
        assert ROLE_ADMIN in CAN_CREATE_ROLES[ROLE_SUPER_ADMIN]
        assert ROLE_USER in CAN_CREATE_ROLES[ROLE_SUPER_ADMIN]
        assert CAN_CREATE_ROLES[ROLE_ADMIN] == [ROLE_USER]
        assert CAN_CREATE_ROLES[ROLE_USER] == []


# ---------------------------------------------------------------------------
# Password hashing tests
# ---------------------------------------------------------------------------

class TestPasswordHashing:
    def test_hash_is_bcrypt_format(self):
        result = _hash_password("secret")
        assert result.startswith("$2")  # bcrypt prefix ($2b$ etc.)

    def test_hash_is_randomized_per_call(self):
        h1 = _hash_password("samepass")
        h2 = _hash_password("samepass")
        assert h1 != h2  # bcrypt salt is random

    def test_different_passwords_produce_different_hashes(self):
        assert _hash_password("pass1") != _hash_password("pass2")

    def test_verify_correct_password(self):
        stored = _hash_password("mypassword")
        assert _verify_password("mypassword", stored) is True

    def test_verify_wrong_password(self):
        stored = _hash_password("mypassword")
        assert _verify_password("wrongpassword", stored) is False

    def test_verify_empty_hash(self):
        assert _verify_password("any", "") is False

    def test_verify_malformed_hash(self):
        assert _verify_password("any", "nocolon") is False

    # ---- Legacy SHA-256 backward-compat ----

    def test_legacy_sha256_detected(self):
        legacy = _legacy_sha256_hash("secret")
        assert _is_legacy_sha256(legacy) is True
        assert _is_legacy_sha256(_hash_password("secret")) is False

    def test_verify_legacy_correct_password(self):
        legacy = _legacy_sha256_hash("legacypass")
        assert _verify_password("legacypass", legacy) is True

    def test_verify_legacy_wrong_password(self):
        legacy = _legacy_sha256_hash("legacypass")
        assert _verify_password("wrong", legacy) is False


class TestLegacyHashInstrumentationAndKillSwitch:
    """Phase 3 #18 — instrumentation + kill switch for the legacy
    SHA-256 verification path. Keep counters moving on every legacy
    verify so operators can confirm nobody's on the old format, and
    give them an env-driven switch to reject legacy hashes once they
    are confident."""

    def _reset_metrics(self):
        from api import metrics
        metrics.reset()

    def setup_method(self):
        self._reset_metrics()

    def teardown_method(self):
        self._reset_metrics()
        import os
        os.environ.pop("AUTH_DISABLE_LEGACY_SHA256", None)

    def test_legacy_success_bumps_success_counter(self):
        from api import metrics
        legacy = _legacy_sha256_hash("correct")
        assert _verify_password("correct", legacy) is True
        snap = metrics.snapshot()
        assert snap.get(
            'legacy_sha256_verifications_total{result="success"}'
        ) == 1

    def test_legacy_wrong_password_bumps_fail_counter(self):
        from api import metrics
        legacy = _legacy_sha256_hash("correct")
        assert _verify_password("wrong", legacy) is False
        snap = metrics.snapshot()
        assert snap.get(
            'legacy_sha256_verifications_total{result="fail"}'
        ) == 1

    def test_bcrypt_verify_does_not_touch_legacy_counter(self):
        from api import metrics
        stored = _hash_password("ok")
        assert _verify_password("ok", stored) is True
        snap = metrics.snapshot()
        assert not any(
            k.startswith("legacy_sha256_verifications_total") for k in snap
        )

    def test_kill_switch_rejects_legacy_even_with_correct_password(
        self, monkeypatch,
    ):
        from api import metrics
        monkeypatch.setenv("AUTH_DISABLE_LEGACY_SHA256", "true")

        legacy = _legacy_sha256_hash("correct")
        assert _verify_password("correct", legacy) is False, (
            "kill switch must reject even a correct legacy password"
        )
        snap = metrics.snapshot()
        assert snap.get(
            'legacy_sha256_verifications_total{result="disabled"}'
        ) == 1
        assert 'legacy_sha256_verifications_total{result="success"}' not in snap

    def test_kill_switch_off_accepts_legacy(self, monkeypatch):
        monkeypatch.delenv("AUTH_DISABLE_LEGACY_SHA256", raising=False)
        legacy = _legacy_sha256_hash("correct")
        assert _verify_password("correct", legacy) is True

    @pytest.mark.parametrize("value", ["false", "0", "", "no", "off"])
    def test_non_truthy_kill_switch_values_keep_legacy_on(
        self, monkeypatch, value,
    ):
        monkeypatch.setenv("AUTH_DISABLE_LEGACY_SHA256", value)
        legacy = _legacy_sha256_hash("correct")
        assert _verify_password("correct", legacy) is True


# ---------------------------------------------------------------------------
# AuthManager tests (mocked Supabase)
# ---------------------------------------------------------------------------

def _mock_supabase():
    """Create a mock Supabase client with chainable table methods."""
    sb = MagicMock()
    return sb


def _make_response(data):
    resp = MagicMock()
    resp.data = data
    return resp


class TestAuthManager:
    @patch("user_authentication.auth_manager.get_supabase")
    def test_authenticate_success(self, mock_get_sb):
        sb = _mock_supabase()
        mock_get_sb.return_value = sb

        stored_hash = _hash_password("secret123")
        sb.table().select().eq().limit().execute.return_value = _make_response([{
            "email": "test@test.com",
            "profile_name": "Tester",
            "password_hash": stored_hash,
            "role": "admin",
        }])

        auth = AuthManager()
        user = auth.authenticate("test@test.com", "secret123")
        assert user is not None
        assert user.email == "test@test.com"
        assert user.role == "admin"

    @patch("user_authentication.auth_manager.get_supabase")
    def test_authenticate_wrong_password(self, mock_get_sb):
        sb = _mock_supabase()
        mock_get_sb.return_value = sb

        stored_hash = _hash_password("secret123")
        sb.table().select().eq().limit().execute.return_value = _make_response([{
            "email": "test@test.com",
            "profile_name": "Tester",
            "password_hash": stored_hash,
            "role": "admin",
        }])

        auth = AuthManager()
        user = auth.authenticate("test@test.com", "wrongpass")
        assert user is None

    @patch("user_authentication.auth_manager.get_supabase")
    def test_authenticate_user_not_found(self, mock_get_sb):
        sb = _mock_supabase()
        mock_get_sb.return_value = sb
        sb.table().select().eq().limit().execute.return_value = _make_response([])

        auth = AuthManager()
        user = auth.authenticate("nobody@test.com", "pass")
        assert user is None

    @patch("user_authentication.auth_manager.get_supabase")
    def test_create_user_success(self, mock_get_sb):
        sb = _mock_supabase()
        mock_get_sb.return_value = sb

        # No existing user
        sb.table().select().eq().limit().execute.return_value = _make_response([])
        sb.table().insert().execute.return_value = _make_response({})

        auth = AuthManager()
        user = auth.create_user("new@test.com", "New User", "pass123", "user")
        assert user.email == "new@test.com"
        assert user.role == "user"

    @patch("user_authentication.auth_manager.get_supabase")
    def test_create_user_duplicate(self, mock_get_sb):
        sb = _mock_supabase()
        mock_get_sb.return_value = sb
        sb.table().select().eq().limit().execute.return_value = _make_response(
            [{"email": "dup@test.com"}]
        )

        auth = AuthManager()
        with pytest.raises(ValueError, match="already exists"):
            auth.create_user("dup@test.com", "Dup", "pass", "user")

    @patch("user_authentication.auth_manager.get_supabase")
    def test_create_user_invalid_role(self, mock_get_sb):
        sb = _mock_supabase()
        mock_get_sb.return_value = sb

        auth = AuthManager()
        with pytest.raises(ValueError, match="Invalid role"):
            auth.create_user("x@test.com", "X", "pass", "invalid_role")

    @patch("user_authentication.auth_manager.get_supabase")
    def test_create_user_missing_fields(self, mock_get_sb):
        sb = _mock_supabase()
        mock_get_sb.return_value = sb

        auth = AuthManager()
        with pytest.raises(ValueError, match="required"):
            auth.create_user("", "Name", "pass", "user")

    @patch("user_authentication.auth_manager.get_supabase")
    def test_list_users(self, mock_get_sb):
        sb = _mock_supabase()
        mock_get_sb.return_value = sb
        sb.table().select().order().execute.return_value = _make_response([
            {"email": "a@test.com", "profile_name": "Alice", "role": "admin"},
            {"email": "b@test.com", "profile_name": "Bob", "role": "user"},
        ])

        auth = AuthManager()
        users = auth.list_users()
        assert len(users) == 2
        assert users[0].profile_name == "Alice"
        assert users[1].role == "user"

    @patch("user_authentication.auth_manager.get_supabase")
    def test_delete_user_success(self, mock_get_sb):
        sb = _mock_supabase()
        mock_get_sb.return_value = sb
        sb.table().select().eq().limit().execute.return_value = _make_response(
            [{"email": "del@test.com"}]
        )
        sb.table().delete().eq().execute.return_value = _make_response({})

        auth = AuthManager()
        auth.delete_user("del@test.com")  # should not raise

    @patch("user_authentication.auth_manager.get_supabase")
    def test_delete_user_not_found(self, mock_get_sb):
        sb = _mock_supabase()
        mock_get_sb.return_value = sb
        sb.table().select().eq().limit().execute.return_value = _make_response([])

        auth = AuthManager()
        with pytest.raises(ValueError, match="not found"):
            auth.delete_user("ghost@test.com")
