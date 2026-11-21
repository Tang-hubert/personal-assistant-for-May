import os
import socket

import httpx
import pytest


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
