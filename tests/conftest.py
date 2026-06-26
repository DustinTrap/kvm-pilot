"""Shared test fixtures and fakes."""

from __future__ import annotations

from typing import Any

import pytest

from kvm_pilot.client import KVMClient


class FakeHTTP:
    """Records requests and returns canned results instead of hitting a network."""

    def __init__(self, results: dict[str, Any] | None = None):
        self.calls: list[dict] = []
        self.results = results or {}
        self._auth_token = None
        self._user = "admin"
        self._passwd = "secret"
        self._base = "https://fake:443"

    def _effective_passwd(self) -> str:
        return self._passwd

    def _record(self, method, path, **kw):
        self.calls.append({"method": method, "path": path, **kw})
        return self.results.get(path, {})

    def get(self, path, params=None, **kw):
        return self._record("GET", path, params=params, **kw)

    def post(self, path, params=None, body=None, content_type=None, **kw):
        return self._record("POST", path, params=params, body=body, **kw)

    def request(self, method, path, **kw):
        return self._record(method, path, **kw)

    def login(self):
        self._auth_token = "tok"
        return "tok"

    def paths(self) -> list[str]:
        return [c["path"] for c in self.calls]


@pytest.fixture
def fake_http():
    return FakeHTTP()


@pytest.fixture
def client(fake_http):
    c = KVMClient("fake", "admin", "secret")
    c._http = fake_http
    return c
