"""Line/frame broadcast TCP server: every connected client receives everything we publish."""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("adsbrx")

DEFAULT_MAX_BUFFER = 1 << 20  # bytes a client may fall behind before it is disconnected


class BroadcastServer:
    def __init__(self, name: str, host: str, port: int, max_buffer: int = DEFAULT_MAX_BUFFER):
        self.name = name
        self.host = host
        self.port = port
        self.max_buffer = max_buffer
        self.bytes_sent = 0
        self.dropped_clients = 0
        self._clients: set[asyncio.StreamWriter] = set()
        self._server: asyncio.Server | None = None

    @property
    def clients(self) -> int:
        return len(self._clients)

    async def start(self) -> None:
        try:
            self._server = await asyncio.start_server(self._serve, self.host, self.port)
        except OSError as exc:
            raise OSError(
                f"cannot listen for {self.name} on {self.host}:{self.port}: {exc.strerror or exc} "
                f"(is another decoder using this port?)"
            ) from exc
        self.port = self._server.sockets[0].getsockname()[1]  # resolves port 0
        log.info("%s output listening on %s:%d", self.name, self.host, self.port)

    def broadcast(self, data: bytes) -> None:
        if not self._clients:
            return
        for writer in list(self._clients):
            transport = writer.transport
            if transport.is_closing():
                self._clients.discard(writer)
            elif transport.get_write_buffer_size() > self.max_buffer:
                log.warning("%s: disconnecting a client that cannot keep up", self.name)
                self.dropped_clients += 1
                self._clients.discard(writer)
                transport.abort()
            else:
                writer.write(data)
                self.bytes_sent += len(data)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
        for writer in list(self._clients):
            writer.close()
        self._clients.clear()
        if self._server is not None:
            try:
                await asyncio.wait_for(self._server.wait_closed(), 2.0)
            except asyncio.TimeoutError:
                pass
            self._server = None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        log.info("%s: client connected %s", self.name, peer)
        self._clients.add(writer)
        try:
            while await reader.read(4096):  # whatever the client sends is ignored; EOF ends the session
                pass
        except (ConnectionError, OSError):
            pass
        finally:
            self._clients.discard(writer)
            writer.close()
            log.info("%s: client disconnected %s", self.name, peer)
