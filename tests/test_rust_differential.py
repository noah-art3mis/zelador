"""Differential test: the Rust spike in rust/ must behave exactly like the Python CLI.

Both implementations run against the same FakeZotero and an identical data
directory. The Python CLI talks to the fake in-process over httpx.MockTransport;
the Rust binary reaches it through a localhost HTTP bridge that forwards every
request to the same handler. The test then asserts equal exit codes, stdout,
stderr, files written, and the sequence of API requests each made.

Skipped when cargo is unavailable or the build fails; built once per session.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

import httpx
import pytest
from typer.testing import CliRunner

from tests.conftest import USER_ID, FakeZotero, make_collection, make_item, make_tag
from zelador import cli
from zelador.client import ZoteroClient
from zelador.config import REPO_ROOT, Credentials

API_KEY = "sekrit-key-123"
CRATE = REPO_ROOT / "rust"
FAKE_ORIGIN = "https://api.zotero.org"
TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{6})?\+00:00")


@pytest.fixture(scope="session")
def rust_bin() -> Path:
    cargo = shutil.which("cargo") or str(Path.home() / ".cargo/bin/cargo")
    if not Path(cargo).exists():
        pytest.skip("cargo not installed")
    # --locked: a test run must never rewrite the tracked Cargo.lock. A toolchain
    # older than rust-version, or a cold build offline, skips with cargo's reason.
    build = subprocess.run(
        [cargo, "build", "--quiet", "--locked"], cwd=CRATE, capture_output=True, text=True
    )
    if build.returncode != 0:
        pytest.skip(f"cargo build failed: {build.stderr.strip()[-500:]}")
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
    return {
        str(p.relative_to(root)): TIMESTAMP.sub("<now>", p.read_text())
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def seed_data_dir(root: Path, strays: bool) -> None:
    backups = root / "backups"
    backups.mkdir(parents=True)
    (backups / "20261001T000000Z.jsonl").write_text(
        '{"kind": "header", "library_version": 10, "timestamp": "20261001T000000Z"}\n'
    )
    lines = [
        {"kind": "header", "library_version": 40, "timestamp": "20261101T120000Z"},
        {"kind": "item", "object": make_item("AAAA1111", tags=[{"tag": "ai"}, {"tag": "ml"}])},
        {"kind": "item", "object": make_item("BBBB2222", tags=[{"tag": "ai"}])},
        {"kind": "collection", "object": make_collection("CCCC0001", "projects")},
        {"kind": "setting", "object": {}},
    ]
    (backups / "20261101T120000Z.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines)
    )
    log = root / "log"
    log.mkdir()
    header = (
        '{"kind": "header", "schema": "log.v1", "plan": "p", "backup": "b", "timestamp": "t"}\n'
    )
    (log / "20261102T000000Z.jsonl").write_text(
        header
        + '{"kind": "entry", "op": "op-001", "status": "pending", "operation": {}}\n'
        + '{"kind": "entry", "op": "op-002", "status": "pending", "operation": {}}\n'
        + '{"kind": "entry", "op": "op-001", "status": "applied", "version": 43}\n'
    )
    (log / "20261103T000000Z.jsonl").write_text(
        "\n"
        + header
        + '{"kind": "entry", "op": "op-001", "status": "pending", "operation": {}}\n'
        + '{"kind": "entry", "op": "op-001", "status": "applied", "version": 44}\n'
    )
    (log / "compress-audit.jsonl").write_text('{"kind": "swap", "file": "x.pdf"}\n')
    (log / "half-written.jsonl").write_text("not json\n")
    audit = root / "audit"
    audit.mkdir()
    for name, ts, version in [
        ("tags", "2026-11-01T10:00:00+00:00", 39),
        ("collections", "2026-11-02T09:00:00.123456+00:00", 41),
    ]:
        (audit / f"{name}.json").write_text(
            json.dumps(
                {
                    "check": name,
                    "library_version": version,
                    "timestamp": ts,
                    "since": None,
                    "findings": [],
                }
            )
        )
    if strays:  # latest_audit tolerates these; the report writer does not
        (audit / "notes.json").write_text('{"unrelated": true}')
        (audit / "broken.json").write_text("{")


def library_fake() -> FakeZotero:
    items = [
        make_item("ITEM0001", version=5, collections=["COLL0001"], title="Filed"),
        make_item("ITEM0002", version=50, title="Ünfiled: l'été"),
        make_item("ITEM0003", version=3, item_type="note"),
        make_item("ITEM0004", version=4, item_type="book", title="Old unfiled"),
        make_item("ITEM0005", version=60, deleted=True, title="Trashed"),
        make_item("ITEM0006", version=6, collections=["COLL0003"]),
    ]
    collections = [
        make_collection("COLL0001", "Projects", version=7),
        make_collection("COLL0002", "projects", version=55),
        make_collection("COLL0003", "Straße", version=8, parent="COLL0001"),
        make_collection("COLL0004", "STRASSE", version=9, parent="COLL0001"),
        make_collection("COLL0005", "Rui's notes", version=10, parent="GONE0001"),
        make_collection("COLL0006", 'say "hi"', version=11, parent="GONE0001"),
        make_collection("COLL0007", 'Say "Hi"', version=12, parent="GONE0001"),
        make_collection("COLL0008", "tab\there's", version=13),
        make_collection("COLL0009", "Tab\tHere's", version=14),
    ]
    tags = [
        make_tag("Machine Learning", 4),
        make_tag("machine-learning", 2, tag_type=1),
        make_tag("machine_learning"),
        make_tag("ÉTÉ"),
        make_tag("été", 3),
        make_tag("solo"),
        make_tag("AI", 9),
        make_tag("a.i.", 1, tag_type=1),
    ]
    return FakeZotero(
        items=items,
        collections=collections,
        tags=tags,
        library_version=77,
        page_size=2,
        emit_next_links=True,
    )


def tags_without_next_links(fake: FakeZotero) -> FakeZotero:
    """Mimic /tags' broken metadata: no next link, so the client must probe."""
    original = fake._paginated

    def paginated(pool, params, path):
        response = original(pool, params, path)
        if path.endswith("/tags"):
            response.headers.pop("Link", None)
        return response

    fake._paginated = paginated
    return fake


def many_tags_probed() -> FakeZotero:
    """A full first page of /tags with no next link: only the probe finds the rest."""
    fake = library_fake()
    fake.tags = (
        [make_tag(f"topic {i:03}") for i in range(140)] + fake.tags + [make_tag("Topic-007")]
    )
    fake.page_size = 100
    return tags_without_next_links(fake)


SCENARIOS = {
    "healthy": library_fake,
    "tags_need_probe": many_tags_probed,
    "retry_then_ok": lambda: _scripted(
        library_fake(), [httpx.Response(429, headers={"Retry-After": "0"})]
    ),
    "forbidden_leaks_key": lambda: _scripted(
        library_fake(), [httpx.Response(403, text=f"Invalid key {API_KEY} " + "x" * 300)]
    ),
    # httpx never follows redirects; a client that does would carry the API key
    # to wherever Location points.
    "redirect_not_followed": lambda: _scripted(
        library_fake(),
        [
            httpx.Response(
                302,
                headers={"Location": f"{FAKE_ORIGIN}/elsewhere", "Last-Modified-Version": "77"},
            )
        ],
    ),
    "backoff_header": lambda: _scripted(
        library_fake(),
        [httpx.Response(200, json=[], headers={"Last-Modified-Version": "77", "Backoff": "0.25"})],
    ),
}


def _scripted(fake: FakeZotero, responses: list[httpx.Response]) -> FakeZotero:
    fake.script.extend(responses)
    return fake


def run_python(args: list[str], fake: FakeZotero, data: Path, monkeypatch) -> dict:
    monkeypatch.setenv("ZELADOR_DATA_DIR", str(data))
    creds = Credentials(api_key=API_KEY, user_id=USER_ID)
    monkeypatch.setattr(
        cli,
        "make_client",
        lambda: ZoteroClient(creds, transport=fake.transport, sleep=lambda s: None),
    )
    result = CliRunner().invoke(cli.app, args)
    return {"exit": result.exit_code, "stdout": result.stdout, "stderr": result.stderr}


def run_rust(binary: Path, args: list[str], fake: FakeZotero, data: Path) -> dict:
    bridge = Bridge(fake)
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(data.parent),
        "ZELADOR_DATA_DIR": str(data),
        "ZELADOR_API_BASE": bridge.origin,
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
        k: TIMESTAMP.sub("<now>", v.replace(str(data), "<data>")) if isinstance(v, str) else v
        for k, v in result.items()
    }


COMMANDS = [
    ["status"],
    ["status", "--json"],
    ["audit", "tags"],
    ["audit", "tags", "--json"],
    ["audit", "collections"],
    ["audit", "collections", "--json"],
    ["audit", "collections", "--since", "40"],
    ["audit", "collections", "--since", "0", "--json"],
    ["audit", "tags", "--since", "40", "--json"],
]


# Python's audit crashes with a traceback on a 3xx page body, so the redirect
# scenario compares only `status`, where the 3xx carries a usable version.
CASES = [
    pytest.param(scenario, args, id=f"{scenario}-{' '.join(args)}")
    for scenario in sorted(SCENARIOS)
    for args in COMMANDS
    if not (scenario == "redirect_not_followed" and args[0] != "status")
]


@pytest.mark.parametrize(("scenario", "args"), CASES)
def test_rust_matches_python(rust_bin, tmp_path, monkeypatch, scenario, args):
    py_data, rs_data = tmp_path / "py" / "data", tmp_path / "rs" / "data"
    seed_data_dir(py_data, strays=args[0] == "status")
    seed_data_dir(rs_data, strays=args[0] == "status")
    py_fake, rs_fake = SCENARIOS[scenario](), SCENARIOS[scenario]()

    expected = normalise(run_python(args, py_fake, py_data, monkeypatch), py_data)
    actual = normalise(run_rust(rust_bin, args, rs_fake, rs_data), rs_data)

    assert actual == expected
    assert snapshot(rs_data) == snapshot(py_data)
    assert request_log(rs_fake) == request_log(py_fake)
    assert API_KEY not in actual["stdout"] + actual["stderr"]
