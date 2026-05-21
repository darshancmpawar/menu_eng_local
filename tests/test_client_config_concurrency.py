"""Tests for optimistic-concurrency on PUT /api/v1/client-config/<name>.

Two admins editing the same client at once used to last-write-wins
silently. GET now returns a ``version`` counter (also in an ``ETag``
response header); PUT must send that version back via either the body
or an ``If-Match`` header, and mismatched versions return 409 with the
current version in the body so the client can refresh + retry.
"""

import pytest

flask = pytest.importorskip("flask", reason="Flask not installed")
from api.app import app
import api.auth as api_auth
from api.auth import issue_token
from user_authentication.models import ROLE_SUPER_ADMIN


@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def _auth_secret(monkeypatch):
    monkeypatch.setattr(api_auth, "API_SECRET_KEY", "test-secret-key")


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {issue_token('test@example.com', ROLE_SUPER_ADMIN)}"}


class TestGetSurfacesVersion:
    def test_body_includes_version(self, client, auth_headers, fake_supabase):
        resp = client.get('/api/v1/client-config/Rippling', headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['version'] == 1

    def test_response_carries_etag(self, client, auth_headers, fake_supabase):
        resp = client.get('/api/v1/client-config/Rippling', headers=auth_headers)
        assert resp.headers['ETag'] == '"1"'


class TestPutRequiresVersion:
    def test_missing_version_returns_400(self, client, auth_headers, fake_supabase):
        resp = client.put(
            '/api/v1/client-config/Rippling',
            json={'theme_map': {'monday': 'mix'}},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert data['success'] is False
        assert 'version is required' in data['error']

    def test_non_integer_version_returns_400(self, client, auth_headers, fake_supabase):
        resp = client.put(
            '/api/v1/client-config/Rippling',
            json={'version': 'one', 'theme_map': {}},
            headers=auth_headers,
        )
        assert resp.status_code == 400


class TestPutAcceptsGoodVersion:
    def test_matching_version_succeeds_and_bumps(
        self, client, auth_headers, fake_supabase,
    ):
        resp = client.put(
            '/api/v1/client-config/Rippling',
            json={'version': 1, 'theme_map': {'monday': 'mix'}},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['version'] == 2
        assert resp.headers['ETag'] == '"2"'

        # A follow-up GET sees the new version.
        resp = client.get('/api/v1/client-config/Rippling', headers=auth_headers)
        assert resp.get_json()['version'] == 2

    def test_if_match_header_also_accepted(
        self, client, auth_headers, fake_supabase,
    ):
        headers = {**auth_headers, 'If-Match': '"1"'}
        resp = client.put(
            '/api/v1/client-config/Rippling',
            json={'theme_map': {'monday': 'mix'}},  # no body version
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.get_json()['version'] == 2


class TestPutRejectsStaleVersion:
    def test_stale_body_version_returns_409_with_current(
        self, client, auth_headers, fake_supabase,
    ):
        # Writer A bumps to 2 first.
        client.put(
            '/api/v1/client-config/Rippling',
            json={'version': 1, 'theme_map': {'monday': 'mix'}},
            headers=auth_headers,
        )
        # Writer B is still holding version=1 from their earlier GET.
        resp = client.put(
            '/api/v1/client-config/Rippling',
            json={'version': 1, 'theme_map': {'tuesday': 'chinese'}},
            headers=auth_headers,
        )
        assert resp.status_code == 409
        data = resp.get_json()
        assert data['success'] is False
        assert 'modified by another request' in data['error']
        assert data['current_version'] == 2

    def test_stale_if_match_header_returns_409(
        self, client, auth_headers, fake_supabase,
    ):
        client.put(
            '/api/v1/client-config/Rippling',
            json={'version': 1, 'theme_map': {'monday': 'mix'}},
            headers=auth_headers,
        )
        headers = {**auth_headers, 'If-Match': '"1"'}
        resp = client.put(
            '/api/v1/client-config/Rippling',
            json={'theme_map': {'tuesday': 'chinese'}},
            headers=headers,
        )
        assert resp.status_code == 409

    def test_conflict_does_not_partially_apply_updates(
        self, client, auth_headers, fake_supabase,
    ):
        """Version-mismatch rejection must happen before any sub-update
        runs, so a losing writer can't leave the DB half-changed."""
        client.put(
            '/api/v1/client-config/Rippling',
            json={'version': 1, 'theme_map': {'monday': 'chinese'}},
            headers=auth_headers,
        )

        # Capture what's in the theme_overrides table after writer A won.
        after_winner = list(fake_supabase.rows('theme_overrides'))

        # Writer B tries with a stale version + a very different theme_map.
        resp = client.put(
            '/api/v1/client-config/Rippling',
            json={
                'version': 1,
                'theme_map': {'tuesday': 'biryani', 'wednesday': 'south'},
            },
            headers=auth_headers,
        )
        assert resp.status_code == 409

        after_loser = list(fake_supabase.rows('theme_overrides'))
        assert after_loser == after_winner, (
            "a rejected PUT must not modify theme_overrides"
        )
