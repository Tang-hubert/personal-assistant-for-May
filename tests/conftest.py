from __future__ import annotations

import asyncio
import asyncio.proactor_events
import asyncio.selector_events
import ipaddress
import socket
import sys
from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest

from src.adapters.errors import NetworkBlockedError
from src.adapters.storage.engine import create_engine
from src.adapters.storage.ledger_repository import SqlLedger
from src.adapters.storage.media_repository import SqlMediaStore
from src.adapters.storage.migrate import upgrade_to_head
from src.adapters.storage.repository import SqlStorage
from src.adapters.storage.session_repository import SqlSessionStore
from src.config import Settings
from tests.fakes import FrozenClock, Owner
from tests.workflow_helpers import WorkflowRig

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_create_connection = asyncio.BaseEventLoop.create_connection
_real_create_datagram_endpoint = asyncio.BaseEventLoop.create_datagram_endpoint
_real_sendto = socket.socket.sendto
_real_sendmsg = getattr(socket.socket, "sendmsg", None)  # absent on Windows


def _blocked(*_args: Any, **_kwargs: Any) -> Any:
    raise NetworkBlockedError("Network blocked in tests (TEST_NO_SEND=1): mock this call")


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


async def _guarded_create_connection(
    self: asyncio.BaseEventLoop,
    protocol_factory: Any,
    host: Any = None,
    port: Any = None,
    **kw: Any,
) -> Any:
    # The Windows proactor loop connects through overlapped I/O, never socket.connect, so the
    # socket patch alone lets asyncio clients (anyio, aiohttp) reach the internet.
    if host is not None and not _is_local((host, port)):
        _blocked()
    return await _real_create_connection(self, protocol_factory, host, port, **kw)


def _guard_sock_connect(
    real: Callable[..., Coroutine[Any, Any, None]],
) -> Callable[..., Coroutine[Any, Any, None]]:
    # The proactor loop connects through ConnectEx (TCP) or WSAConnect (UDP), not socket.connect.
    async def guarded(self: asyncio.AbstractEventLoop, sock: socket.socket, address: Any) -> None:
        if not _is_local(address):
            _blocked()
        await real(self, sock, address)

    return guarded


async def _guarded_create_datagram_endpoint(
    self: asyncio.BaseEventLoop,
    protocol_factory: Any,
    local_addr: Any = None,
    remote_addr: Any = None,
    **kw: Any,
) -> Any:
    if remote_addr is not None and not _is_local(remote_addr):
        _blocked()
    return await _real_create_datagram_endpoint(
        self, protocol_factory, local_addr, remote_addr, **kw
    )


def _guarded_sendto(self: socket.socket, data: Any, *args: Any) -> int:
    # sendto(data, address) or sendto(data, flags, address): the address is always last.
    if not _is_local(args[-1]):
        _blocked()
    return _real_sendto(self, data, *args)


def _guarded_sendmsg(
    self: socket.socket, buffers: Any, ancdata: Any = (), flags: int = 0, address: Any = None
) -> int:
    if address is not None and not _is_local(address):
        _blocked()
    assert _real_sendmsg is not None
    return int(_real_sendmsg(self, buffers, ancdata, flags, address))


def _guard_iocp_sendto(real: Callable[..., Any]) -> Callable[..., Any]:
    # Proactor UDP (loop.sock_sendto, datagram transports) sends through WSASendTo directly.
    def guarded(self: Any, conn: Any, buf: Any, flags: int = 0, addr: Any = None) -> Any:
        if addr is not None and not _is_local(addr):
            _blocked()
        return real(self, conn, buf, flags, addr)

    return guarded


@pytest.fixture(autouse=True)
def _offline_test_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Real keys may be in the environment (compose env_file); tests must never see them.
    for name in Settings.model_fields:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("TEST_NO_SEND", "1")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _blocked)
    # google-genai and openai ship on httpx2, a fork under its own import namespace.
    monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", _blocked)
    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", _blocked)
    monkeypatch.setattr(asyncio.BaseEventLoop, "create_connection", _guarded_create_connection)
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded_connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", _guarded_sendto)
    if _real_sendmsg is not None:
        monkeypatch.setattr(socket.socket, "sendmsg", _guarded_sendmsg)
    monkeypatch.setattr(
        asyncio.BaseEventLoop, "create_datagram_endpoint", _guarded_create_datagram_endpoint
    )
    for loop_cls in (
        asyncio.proactor_events.BaseProactorEventLoop,
        asyncio.selector_events.BaseSelectorEventLoop,
    ):
        monkeypatch.setattr(loop_cls, "sock_connect", _guard_sock_connect(loop_cls.sock_connect))
    if sys.platform == "win32":
        iocp = asyncio.windows_events.IocpProactor
        monkeypatch.setattr(iocp, "sendto", _guard_iocp_sendto(iocp.sendto))


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


@pytest.fixture
async def wf(
    storage: SqlStorage,
    media_store: SqlMediaStore,
    db_path: Path,
    clock: FrozenClock,
    owner: Owner,
) -> AsyncIterator[WorkflowRig]:
    engine = create_engine(db_path)
    yield WorkflowRig(
        storage,
        media_store,
        SqlSessionStore(engine, clock),
        SqlLedger(engine, clock),
        clock,
        owner,
        db_path,
    )
    await engine.dispose()
