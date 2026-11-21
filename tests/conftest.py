from __future__ import annotations

import ipaddress
import socket
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.adapters.storage.engine import create_engine
from src.adapters.storage.media_repository import SqlMediaStore
from src.adapters.storage.migrate import upgrade_to_head
from src.adapters.storage.repository import SqlStorage
from src.config import Settings
from tests.fakes import FrozenClock, Owner

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _blocked(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("Network blocked in tests (TEST_NO_SEND=1): mock this call")


def _is_local(address: Any) -> bool:
    if not isinstance(address, tuple):
        return True  # AF_UNIX path
    host = address[0]
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _guarded_connect(self: socket.socket, address: Any) -> None:
    # Windows asyncio builds its self-pipe with a loopback socketpair, so loopback stays open.
    if not _is_local(address):
        _blocked()
    _real_connect(self, address)


def _guarded_connect_ex(self: socket.socket, address: Any) -> int:
    if not _is_local(address):
        _blocked()
    return _real_connect_ex(self, address)


@pytest.fixture(autouse=True)
def _offline_test_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Real keys may be in the environment (compose env_file); tests must never see them.
    for name in Settings.model_fields:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("TEST_NO_SEND", "1")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _blocked)
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded_connect_ex)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 11, 21, 3, 0, tzinfo=UTC))


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    upgrade_to_head(path)
    return path


@pytest.fixture
async def storage(db_path: Path, clock: FrozenClock) -> AsyncIterator[SqlStorage]:
    engine = create_engine(db_path)
    yield SqlStorage(engine, clock)
    await engine.dispose()


@pytest.fixture
async def media_store(
    db_path: Path, clock: FrozenClock, tmp_path: Path
) -> AsyncIterator[SqlMediaStore]:
    engine = create_engine(db_path)
    yield SqlMediaStore(engine, clock, tmp_path / "data")
    await engine.dispose()


@pytest.fixture
async def owner(storage: SqlStorage) -> Owner:
    tenant_id = await storage.create_tenant(name="May")
    return Owner(tenant_id, await storage.create_user(tenant_id=tenant_id, display_name="May"))


@pytest.fixture
async def other_owner(storage: SqlStorage) -> Owner:
    tenant_id = await storage.create_tenant(name="Other")
    return Owner(tenant_id, await storage.create_user(tenant_id=tenant_id, display_name="Other"))
