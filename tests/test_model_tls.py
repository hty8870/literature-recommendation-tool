from __future__ import annotations

import asyncio
import os
import shutil
import ssl
import subprocess  # noqa: S404 - OpenSSL generates an ephemeral fixture certificate.

import httpcore
import httpx
import pytest

from citation_tool import model_egress


async def test_real_tls_checks_original_host_after_ip_pinning(tmp_path, monkeypatch):
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("OpenSSL CLI not installed")
    certificate, key = tmp_path / "fixture.pem", tmp_path / "fixture-key.pem"
    completed = await asyncio.to_thread(
        subprocess.run,  # noqa: S603
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=provider.example.net",
            "-addext",
            "subjectAltName=DNS:provider.example.net",
            "-keyout",
            os.fspath(key),
            "-out",
            os.fspath(certificate),
        ],
        capture_output=True,
        timeout=15,
    )
    assert completed.returncode == 0
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certificate, key)
    client_context = ssl.create_default_context(cafile=os.fspath(certificate))
    tls_names, received, destinations = [], [], []
    server_context.set_servername_callback(lambda _, name, _ctx: tls_names.append(name))

    async def read_request(reader, writer):
        async with asyncio.timeout(3):
            try:
                received.append(await reader.readuntil(b"\r\n\r\n"))
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

    real_connect = httpcore.AnyIOBackend.connect_tcp
    real_transport = httpx.AsyncHTTPTransport
    server = await asyncio.start_server(read_request, "127.0.0.1", 0, ssl=server_context)
    port = server.sockets[0].getsockname()[1]

    async def connect(self, host, requested_port, *args, **kwargs):
        destinations.append((host, requested_port))
        return await real_connect(self, "127.0.0.1", port, *args, **kwargs)

    async def resolve(_host):
        return ["8.8.8.8"]

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    monkeypatch.setattr(model_egress, "_resolve_public", resolve)
    monkeypatch.setattr(
        model_egress.httpx,
        "AsyncHTTPTransport",
        lambda **kwargs: real_transport(verify=client_context, **kwargs),
    )
    async with server:
        base = "https://provider.example.net/v1"
        transport = model_egress.PublicModelTransport(
            base, "responses", timeout_s=3, api_key="fixture"
        )
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            response = await client.post(base + "/responses", json={})
            assert response.json() == {}
        wrong_base = "https://bad-name.example.net/v1"
        wrong = model_egress.PublicModelTransport(
            wrong_base, "responses", timeout_s=3, api_key="fixture"
        )
        async with httpx.AsyncClient(transport=wrong, trust_env=False) as client:
            with pytest.raises(httpx.ConnectError, match="verified TLS"):
                await client.post(wrong_base + "/responses", json={})
    assert destinations == [("8.8.8.8", 443), ("8.8.8.8", 443)]
    assert tls_names == ["provider.example.net", "bad-name.example.net"]
    assert len(received) == 1 and b"host: provider.example.net" in received[0].lower()
