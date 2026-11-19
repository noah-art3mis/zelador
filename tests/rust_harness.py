"""Shared harness for the Rust differential tests: both CLIs against one FakeZotero.

The Python CLI talks to the fake in-process over httpx.MockTransport; the Rust
binary reaches it through a localhost HTTP bridge that forwards every request to
the same handler. Both run with the clock frozen and one key sequence.
"""

from __future__ import annotations

import itertools
import os
import shutil
import subprocess
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

import httpx
import pytest
from typer.testing import CliRunner

from tests.conftest import REPO_ROOT, USER_ID, FakeZotero
from zelador import cli
from zelador.client import ZoteroClient
from zelador.config import Credentials

API_KEY = "sekrit-key-123"
CRATE = REPO_ROOT / "rust"
FAKE_ORIGIN = "https://api.zotero.org"
# Both implementations run with the clock frozen at one instant, so timestamps,
# backup ids and plan ids compare exactly. The zero-microsecond instant pins
# isoformat() dropping the fraction.
INSTANTS = {
    "default": datetime(2026, 11, 18, 12, 34, 56, 789012, tzinfo=UTC),
    "configured": datetime(2026, 11, 18, 12, 34, 56, tzinfo=UTC),
}
# Client-generated object keys (create_item, create_collection) are random in
# production; both implementations draw them from one deterministic sequence here.
KEY_PREFIX = "NEWC"  # the golden fixture's keygen (tests/test_golden_plan.py)
CLOCK_MODULES = (
    "zelador.backup",
    "zelador.audit.runner",
    "zelador.cli_change",
    "zelador.cli_compress",
)


# A fixed non-UTC zone (POSIX form, no tz database needed), so a timestamp taken
# in local time instead of UTC differs by three hours on every machine, CI included.
TEST_TZ = "<-03>3"


@pytest.fixture
def test_tz():
    """TEST_TZ for this test only. Set by hand, not with monkeypatch: tzset() must
    run again after TZ is restored, or the zone outlives the test."""
    saved = os.environ.get("TZ")
    os.environ["TZ"] = TEST_TZ
    time.tzset()
    yield
    if saved is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = saved
    time.tzset()


def freeze_python_clock(monkeypatch, instant: datetime) -> None:
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            # Naive now() is local wall time, as the real one is.
            return instant if tz is not None else instant.astimezone().replace(tzinfo=None)

    for name in CLOCK_MODULES:
        monkeypatch.setattr(f"{name}.datetime", Frozen)


@pytest.fixture(scope="session")
def rust_bin() -> Path:
    cargo = shutil.which("cargo") or str(Path.home() / ".cargo/bin/cargo")
    if not Path(cargo).exists():
        pytest.skip("cargo not installed")
    # --locked: a test run must never rewrite the tracked Cargo.lock. Only a
    # missing toolchain skips; a build failure is a failure, or a compile error
    # in the crate would turn this whole suite into a green skip.
    build = subprocess.run(
        [cargo, "build", "--quiet", "--locked"], cwd=CRATE, capture_output=True, text=True
    )
    if build.returncode != 0:
        pytest.fail(f"cargo build failed:\n{build.stderr.strip()[-2000:]}", pytrace=False)
    return CRATE / "target" / "debug" / "zel"


class Bridge:
    """Localhost HTTP server forwarding each request to a FakeZotero handler."""

    def __init__(self, fake: FakeZotero):
        self.fake = fake
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def _forward(self):
                length = int(self.headers.get("Content-Length") or 0)
                request = httpx.Request(
                    self.command,
                    FAKE_ORIGIN + self.path,
                    headers=dict(self.headers),
                    content=self.rfile.read(length) if length else b"",
                )
                response = bridge.fake.handler(request)
                body = response.content
                self.send_response(response.status_code)
                for name, value in response.headers.items():
                    if name.lower() in ("content-length", "transfer-encoding"):
                        continue
                    if name.lower() in ("link", "location"):
                        value = value.replace(FAKE_ORIGIN, bridge.origin)
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = _forward

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def request_log(fake: FakeZotero) -> list[tuple]:
    """What each implementation asked the API for, independent of URL encoding."""
    return [
        (
            r.method,
            r.url.path,
            tuple(sorted(parse_qsl(urlparse(str(r.url)).query))),
            r.headers.get("Zotero-API-Version"),
            r.headers.get("Zotero-API-Key"),
            r.headers.get("If-Modified-Since-Version"),
        )
        for r in fake.requests
    ]


def snapshot(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): p.read_text() for p in sorted(root.rglob("*")) if p.is_file()}


def _scripted(fake: FakeZotero, responses: list[httpx.Response]) -> FakeZotero:
    fake.script.extend(responses)
    return fake


def run_python(
    args: list[str], fake: FakeZotero, data: Path, monkeypatch, instant: datetime
) -> dict:
    monkeypatch.setenv("ZELADOR_DATA_DIR", str(data))
    freeze_python_clock(monkeypatch, instant)
    # Rust's debug-only ZELADOR_KEYGEN_PREFIX produces this same sequence.
    counter = itertools.count(1)
    monkeypatch.setattr(
        "zelador.write.expand._generate_key", lambda: f"{KEY_PREFIX}{next(counter):04d}"
    )
    creds = Credentials(api_key=API_KEY, user_id=USER_ID)
    monkeypatch.setattr(
        cli,
        "make_client",
        lambda: ZoteroClient(creds, transport=fake.transport, sleep=lambda s: None),
    )
    result = CliRunner().invoke(cli.app, args)
    return {"exit": result.exit_code, "stdout": result.stdout, "stderr": result.stderr}


def run_rust(
    binary: Path, args: list[str], fake: FakeZotero, data: Path, instant: datetime
) -> dict:
    bridge = Bridge(fake)
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(data.parent),
        "ZELADOR_DATA_DIR": str(data),
        "ZELADOR_API_BASE": bridge.origin,
        "ZELADOR_NOW": instant.isoformat(),
        "ZELADOR_KEYGEN_PREFIX": KEY_PREFIX,
        "TZ": TEST_TZ,
        "ZELADOR_CONFIG_DIR": os.environ["ZELADOR_CONFIG_DIR"],  # conftest's per-test dir
        "ZOTERO_API_KEY": API_KEY,
        "ZOTERO_USER_ID": USER_ID,
    }
    try:
        proc = subprocess.run(
            [str(binary), *args], env=env, capture_output=True, text=True, timeout=60
        )
    finally:
        bridge.close()
    return {"exit": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


def normalise(result: dict, data: Path) -> dict:
    return {
        k: v.replace(str(data), "<data>") if isinstance(v, str) else v for k, v in result.items()
    }
