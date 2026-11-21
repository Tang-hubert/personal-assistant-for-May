import asyncio
import os
import socket

import httpx
import httpx2
import pytest
from google import genai
from openai import AsyncOpenAI

from src.config import DEEPSEEK_BASE_URL


def test_env_is_forced_to_test_mode() -> None:
    assert os.environ["APP_ENV"] == "test"
    assert os.environ["TEST_NO_SEND"] == "1"


def test_sync_httpx_call_is_blocked() -> None:
    with pytest.raises(RuntimeError, match="Network blocked"):
        httpx.get("https://example.com")


async def test_async_httpx_call_is_blocked() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(RuntimeError, match="Network blocked"):
            await client.get("https://example.com")


def test_raw_socket_to_public_host_is_blocked() -> None:
    with pytest.raises(RuntimeError, match="Network blocked"):
        socket.create_connection(("1.1.1.1", 443), timeout=1)


def test_loopback_socket_is_allowed() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        client = socket.create_connection(server.getsockname(), timeout=1)
        client.close()
    finally:
        server.close()


def _chain(error: BaseException) -> list[BaseException]:
    links: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in links:
        links.append(current)
        current = current.__cause__ or current.__context__
    return links


def _blocked_by_guard(error: BaseException) -> bool:
    return any(
        isinstance(link, RuntimeError) and "Network blocked" in str(link) for link in _chain(error)
    )


async def test_unmocked_openai_client_is_blocked() -> None:
    client = AsyncOpenAI(api_key="test", base_url=DEEPSEEK_BASE_URL, max_retries=0)
    with pytest.raises(Exception) as caught:
        await client.chat.completions.create(
            model="deepseek-flash", messages=[{"role": "user", "content": "hi"}]
        )
    assert _blocked_by_guard(caught.value)


async def test_unmocked_genai_client_is_blocked() -> None:
    client = genai.Client(api_key="test")
    with pytest.raises(Exception) as caught:
        await client.aio.models.generate_content(model="gemini-3.8-flash", contents="hi")
    assert _blocked_by_guard(caught.value)


async def test_unmocked_httpx2_client_is_blocked() -> None:
    async with httpx2.AsyncClient() as client:
        with pytest.raises(RuntimeError, match="Network blocked"):
            await client.get("https://example.com")


async def test_raw_asyncio_connection_to_public_host_is_blocked() -> None:
    with pytest.raises(RuntimeError, match="Network blocked"):
        await asyncio.open_connection("1.1.1.1", 443)


TEST_NET = ("192.0.2.1", 443)  # RFC 5737: reserved, never routed
# Without SO_BROADCAST the OS refuses this destination locally, so even an unguarded send
# never leaves the machine.
BROADCAST = ("255.255.255.255", 9)


def _tcp_server() -> socket.socket:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    return server


def _no_proactor_io(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Replace the running proactor's connect so an unguarded call is recorded, not sent."""
    calls: list[object] = []
    proactor = getattr(asyncio.get_running_loop(), "_proactor", None)
    if proactor is not None:

        def spy(conn: object, address: object) -> None:
            calls.append(address)
            raise AssertionError(f"proactor connect reached for {address}")

        monkeypatch.setattr(proactor, "connect", spy)
    return calls


async def test_loop_sock_connect_to_public_host_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _no_proactor_io(monkeypatch)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setblocking(False)
        with pytest.raises(RuntimeError, match="Network blocked"):
            await asyncio.get_running_loop().sock_connect(sock, TEST_NET)
    assert calls == []


def test_selector_loop_sock_connect_to_public_host_is_blocked() -> None:
    async def connect() -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setblocking(False)
            await asyncio.get_running_loop().sock_connect(sock, TEST_NET)

    loop = asyncio.SelectorEventLoop()
    try:
        with pytest.raises(RuntimeError, match="Network blocked"):
            loop.run_until_complete(connect())
    finally:
        loop.close()


async def test_loop_sock_connect_to_loopback_is_allowed() -> None:
    server = _tcp_server()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setblocking(False)
            await asyncio.get_running_loop().sock_connect(sock, server.getsockname())
    finally:
        server.close()


def test_udp_sendto_public_host_is_blocked() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        with pytest.raises(RuntimeError, match="Network blocked"):
            sock.sendto(b"x", BROADCAST)
        with pytest.raises(RuntimeError, match="Network blocked"):
            sock.sendto(b"x", 0, BROADCAST)


async def test_loop_sock_sendto_public_host_is_blocked() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setblocking(False)
        with pytest.raises(RuntimeError, match="Network blocked"):
            await asyncio.get_running_loop().sock_sendto(sock, b"x", BROADCAST)


async def test_datagram_endpoint_to_public_host_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _no_proactor_io(monkeypatch)
    with pytest.raises(RuntimeError, match="Network blocked"):
        await asyncio.get_running_loop().create_datagram_endpoint(
            asyncio.DatagramProtocol, remote_addr=TEST_NET
        )
    assert calls == []


async def test_loopback_udp_is_allowed() -> None:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver,
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender,
    ):
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(2)
        sender.sendto(b"ping", receiver.getsockname())
        assert receiver.recv(16) == b"ping"

        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            asyncio.DatagramProtocol, remote_addr=receiver.getsockname()
        )
        transport.sendto(b"pong")
        await asyncio.sleep(0.05)
        transport.close()
        assert receiver.recv(16) == b"pong"
