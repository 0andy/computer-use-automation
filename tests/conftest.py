"""Shared pytest fixtures.

``mockbank_server`` starts the MockBank FastAPI app with uvicorn in a test-controlled
thread on a random free port, exposes its base URL, sets ``MOCKBANK_BASE_URL`` so app
config can be pointed at it, and shuts the server down at the end of the session.
No test needs a manually started server, and no test needs an API key.

``browser`` (session-scoped, headless Chromium, sync API) with ``context``/``page``
(function-scoped) drive that server in integration tests.
"""

from __future__ import annotations

import builtins
import os
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
import uvicorn
from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from mockbank import faults
from mockbank.app import app as mockbank_app

STARTUP_TIMEOUT_S = 15.0
SHUTDOWN_TIMEOUT_S = 15.0


@dataclass(frozen=True)
class MockBankServer:
    base_url: str  # e.g. http://127.0.0.1:54321 (no trailing slash)

    def url(self, path: str) -> str:
        return self.base_url + path


@pytest.fixture(scope="session")
def mockbank_server() -> Iterator[MockBankServer]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))  # random free port, chosen by the OS
    port = sock.getsockname()[1]

    config = uvicorn.Config(
        mockbank_app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        log_config=None,
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, name="mockbank-uvicorn", daemon=True
    )
    thread.start()

    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("MockBank test server failed to start")
        time.sleep(0.02)

    base_url = f"http://127.0.0.1:{port}"
    previous = os.environ.get("MOCKBANK_BASE_URL")
    os.environ["MOCKBANK_BASE_URL"] = base_url
    try:
        yield MockBankServer(base_url=base_url)
    finally:
        if previous is None:
            os.environ.pop("MOCKBANK_BASE_URL", None)
        else:
            os.environ["MOCKBANK_BASE_URL"] = previous
        server.should_exit = True
        thread.join(timeout=SHUTDOWN_TIMEOUT_S)


@pytest.fixture(autouse=True)
def _disarm_faults() -> Iterator[None]:
    """Fault state is process-wide; never let a fault armed by one test leak into another."""
    faults.reset()
    yield
    faults.reset()


CONSOLE_INPUT_GUARD = "builtins.input() must never be reached under pytest: use ScriptedOperator"


@pytest.fixture(autouse=True)
def _no_console_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may block on console input (spec 15.2/15.3): ConsoleOperator is for the headed demo only."""

    def guard(*_args: object, **_kwargs: object) -> str:
        raise AssertionError(CONSOLE_INPUT_GUARD)

    monkeypatch.setattr(builtins, "input", guard)


# --- Playwright (sync API, headless) for integration tests ---


@pytest.fixture(scope="session")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(headless=True)
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture
def context(browser: Browser) -> Iterator[BrowserContext]:
    ctx = browser.new_context()
    try:
        yield ctx
    finally:
        ctx.close()


@pytest.fixture
def page(context: BrowserContext) -> Page:
    return context.new_page()
