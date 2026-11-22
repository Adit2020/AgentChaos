"""Serve one ITSM fixture on an ephemeral loopback port."""

import asyncio
import socket
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

import uvicorn

from .api import create_app
from .database import Environment
from .service_fault import AssignmentMonitor


class _FixtureServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        # The surrounding API server or application owns process signals.
        yield


@asynccontextmanager
async def serve_fixture(
    environment: Environment, *, monitor: AssignmentMonitor | None = None
) -> AsyncIterator[str]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = _FixtureServer(
            uvicorn.Config(
                create_app(environment, monitor=monitor),
                proxy_headers=False,
                log_config=None,
                access_log=False,
                lifespan="off",
                ws="none",
                workers=1,
            )
        )
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if serving.done():
                        serving.result()
                        raise RuntimeError(
                            "ITSM fixture listener exited during startup"
                        )
                    await asyncio.sleep(0.01)
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            # Drain requests before the harness takes its final snapshot. Shield
            # the server from cancellation, including cancellation during drain.
            cancelled = False
            while not serving.done():
                try:
                    await asyncio.shield(serving)
                except asyncio.CancelledError:
                    cancelled = True
            serving.result()
            if cancelled:
                raise asyncio.CancelledError
