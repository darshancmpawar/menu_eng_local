"""Tests for ``ui.backend_probe`` — backend-port picking helpers.

The probes must not hand the UI a port that is serving a foreign
service, and must fail loudly when every candidate is occupied.
"""

import socket
from unittest.mock import patch

import pytest

from ui.backend_probe import (
    BACKEND_PORT_CANDIDATES,
    health_check,
    pick_backend_port,
    port_is_bindable,
)


class TestPortIsBindable:
    def test_free_port_is_bindable(self):
        # Reserve an ephemeral port, then release it. The port should be
        # bindable again immediately after.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        assert port_is_bindable(port) is True

    def test_occupied_port_is_not_bindable(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            port = s.getsockname()[1]
            assert port_is_bindable(port) is False


class TestHealthCheck:
    def test_unreachable_port_returns_false(self):
        # Port 1 is never open for HTTP; ConnectionRefusedError → False.
        assert health_check(1) is False

    def test_non_json_response_returns_false(self):
        class FakeResp:
            status_code = 200

            def json(self):
                raise ValueError("not json")

        with patch("ui.backend_probe.requests.get", return_value=FakeResp()):
            assert health_check(5000) is False

    def test_wrong_status_field_returns_false(self):
        class FakeResp:
            status_code = 200

            def json(self):
                return {"status": "something-else"}

        with patch("ui.backend_probe.requests.get", return_value=FakeResp()):
            assert health_check(5000) is False

    def test_healthy_response_returns_true(self):
        class FakeResp:
            status_code = 200

            def json(self):
                return {"status": "healthy"}

        with patch("ui.backend_probe.requests.get", return_value=FakeResp()):
            assert health_check(5000) is True


class TestPickBackendPort:
    def test_prefers_existing_backend(self):
        candidates = list(BACKEND_PORT_CANDIDATES)
        target = candidates[2]
        with patch("ui.backend_probe.health_check",
                   side_effect=lambda p: p == target):
            assert pick_backend_port() == target

    def test_falls_back_to_first_bindable(self):
        candidates = list(BACKEND_PORT_CANDIDATES)
        target = candidates[1]
        with patch("ui.backend_probe.health_check", return_value=False), \
             patch("ui.backend_probe.port_is_bindable",
                   side_effect=lambda p: p == target):
            assert pick_backend_port() == target

    def test_raises_when_all_ports_occupied(self):
        with patch("ui.backend_probe.health_check", return_value=False), \
             patch("ui.backend_probe.port_is_bindable", return_value=False):
            with pytest.raises(RuntimeError, match="occupied"):
                pick_backend_port()
