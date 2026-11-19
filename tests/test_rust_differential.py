"""Differential test: the Rust spike in rust/ must behave exactly like the Python CLI.

Both implementations run against the same FakeZotero and an identical data
directory. The Python CLI talks to the fake in-process over httpx.MockTransport;
the Rust binary reaches it through a localhost HTTP bridge that forwards every
request to the same handler. The test then asserts equal exit codes, stdout,
stderr, files written, and the sequence of API requests each made.

Skipped when cargo is not installed; a failed build fails every case.
"""

from __future__ import annotations

import itertools
import json
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

from tests.conftest import REPO_ROOT, USER_ID, FakeZotero, make_collection, make_item, make_tag
from zelador import cli, config
from zelador.client import ZoteroClient
from zelador.config import Credentials

GOLDEN_DIR = Path(__file__).parent / "golden"

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
        make_item(
            "ITEM0001",
            version=5,
            collections=["COLL0001"],
            title="Filed",
            # Numbers whose JSON text Rust must reproduce exactly as Python does.
            numbers={"small": 1.5e-07, "big": 123456789012345678901234, "negzero": -0.0},
            nothing=None,
        ),
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
        settings={"tagColors": {"value": [{"name": "Ünread", "color": "#E69F00"}], "version": 70}},
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


def seed_config_dir(path: Path) -> None:
    """A real config.yaml and registry, read identically by both implementations."""
    path.mkdir(parents=True)
    (path / "config.yaml").write_text("style: chicago-note-bibliography\ncitekey_sources: []\n")
    shutil.copy(REPO_ROOT / "taxonomy.example.yaml", path / "taxonomy.yaml")


def unchanged_since_backup() -> FakeZotero:
    """The library still at the seeded backup's version: backup is a verified no-op."""
    fake = library_fake()
    fake.library_version = 40
    return fake


def foreign_number_text() -> FakeZotero:
    """The real API is not Python: its number text (0.00001, 1E5, -0, 1.50) is not
    what json.dumps would write, so Rust must re-render it, not copy it."""
    item = make_item("ITEM0099", version=60, collections=["COLL0001"], numbers="@N@")
    raw = json.dumps([item]).replace(
        '"@N@"', '{"tiny": 0.00001, "upper": 1E5, "negzero": -0, "padded": 1.50}'
    )
    fake = library_fake()
    fake.script.append(
        httpx.Response(
            200,
            content=raw.encode(),
            headers={"Content-Type": "application/json", "Last-Modified-Version": "77"},
        )
    )
    return fake


SCENARIOS = {
    "healthy": library_fake,
    "foreign_number_text": foreign_number_text,
    "unchanged_since_backup": unchanged_since_backup,
    "configured": library_fake,  # plus seed_config_dir before both runs
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


COMMANDS = [
    ["backup"],
    ["backup", "--json"],
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
    # The scripted page answers the first request; only backup's is the item dump
    # whose numbers reach the output.
    and not (scenario == "foreign_number_text" and args[0] != "backup")
]


@pytest.mark.parametrize(("scenario", "args"), CASES)
def test_rust_matches_python(
    rust_bin, tmp_path, monkeypatch, isolated_config_dir, test_tz, scenario, args
):
    py_data, rs_data = tmp_path / "py" / "data", tmp_path / "rs" / "data"
    seed_data_dir(py_data, strays=args[0] == "status")
    seed_data_dir(rs_data, strays=args[0] == "status")
    if scenario == "configured":
        seed_config_dir(isolated_config_dir)
    py_fake, rs_fake = SCENARIOS[scenario](), SCENARIOS[scenario]()

    instant = INSTANTS.get(scenario, INSTANTS["default"])
    expected = normalise(run_python(args, py_fake, py_data, monkeypatch, instant), py_data)
    actual = normalise(run_rust(rust_bin, args, rs_fake, rs_data, instant), rs_data)

    assert actual == expected
    assert snapshot(rs_data) == snapshot(py_data)
    assert request_log(rs_fake) == request_log(py_fake)
    assert API_KEY not in actual["stdout"] + actual["stderr"]


# -- zel validate ----------------------------------------------------------------

# The registry every validate case expands against unless it says otherwise.
REGISTRY = """\
families:
  status: {description: workflow state, coloured: true, exclusive: true}
  rating: {coloured: true, exclusive: true}
  topic: {description: subject vocabulary}
tags:
  - tag: status:to-read
    aliases: [to-read, unread]
    colour: "#E69F00"
  - tag: status:read
    aliases: [read]
    colour: "#009E73"
  - tag: rating:fav
    colour: "#cc79a7"
  - tag: topic:ai
    aliases: [AI, Artificial Intelligence, "l'été"]
  - tag: topic:ml
"""
REGISTRY_COLOURS = [
    {"name": "status:to-read", "color": "#E69F00"},
    {"name": "status:read", "color": "#009E73"},
    {"name": "rating:fav", "color": "#cc79a7"},
]

# The golden fixture's registry (tests/test_golden_plan.py), as a file.
GOLDEN_REGISTRY = """\
families:
  status: {coloured: true, exclusive: true}
  topic: {}
tags:
  - {tag: status:to-read, colour: "#E69F00"}
  - {tag: status:read, colour: "#009E73"}
  - {tag: topic:ai, aliases: [AI, Artificial Intelligence]}
"""


def validate_fake(tag_colors=REGISTRY_COLOURS) -> FakeZotero:
    """A library with every shape validate distinguishes: items with and without
    a field, creators or tags, every attachment state, a type the schema
    endpoints refuse, a stock exclusivity violation, and a collection tree."""
    items = [
        make_item(
            "JOUR0001",
            version=5,
            title="Attention",
            tags=[{"tag": "AI", "type": 1}, {"tag": "keep-me"}],
            collections=["COLL0001"],
            DOI="10.1/x",
            creators=[{"creatorType": "author", "firstName": "Ashish", "lastName": "Vaswani"}],
        ),
        make_item(
            "JOUR0002",
            version=9,
            title="Deep learning",
            tags=[{"tag": "status:to-read"}, {"tag": "l'été"}],
            creators=[],
        ),
        make_item(
            "BOOK0001",
            version=11,
            item_type="book",
            title="Knowledge",
            publisher="Routledge",
            creators=[{"creatorType": "editor", "name": "Ed Itor"}],
        ),
        make_item(
            "WEBP0001",
            version=12,
            item_type="webpage",
            title="Blog",
            websiteTitle="Site",
            creators=[{"creatorType": "contributor", "name": "Cont"}],
        ),
        make_item(
            "ATTC0001",
            version=13,
            item_type="attachment",
            title="scan.pdf",
            tags=[{"tag": "unread"}, {"tag": "topic:ai"}],
            collections=["COLL0002", "COLL0003"],
        ),
        make_item("ATTC0002", version=14, item_type="attachment", title="bare.pdf"),
        make_item(
            "ATTC0003", version=15, item_type="attachment", title="kid.pdf", parentItem="JOUR0001"
        ),
        make_item("NOTE0001", version=16, item_type="note", title=""),
        make_item(
            "DUPL0001",
            version=17,
            title="Two statuses",
            tags=[{"tag": "status:to-read"}, {"tag": "status:read"}],
        ),
        make_item("TRSH0001", version=18, title="Trashed", deleted=True),
    ]
    collections = [
        make_collection("COLL0001", "Projects", version=3),
        make_collection("COLL0002", "Inbox", version=4),
        make_collection("COLL0003", "Thesis", version=5, parent="COLL0001"),
        make_collection("COLL0004", "Chapter 1", version=6, parent="COLL0003"),
        make_collection("COLL0005", "Old", version=7),
    ]
    collections[-1]["data"]["deleted"] = True
    settings = {} if tag_colors is None else {"tagColors": {"value": tag_colors, "version": 280}}
    return FakeZotero(
        items=items,
        collections=collections,
        settings=settings,
        library_version=300,
        page_size=3,
    )


def golden_fake() -> FakeZotero:
    from tests.test_golden_plan import library

    return library()


def cs(*intents, slug="case", **extra) -> dict:
    return {"schema": "changeset.v1", "slug": slug, "intents": list(intents), **extra}


def creator(**fields) -> dict:
    return {"creatorType": "author", **fields}


VALIDATE_CASES: dict[str, dict] = {
    # -- every op, one at a time ------------------------------------------------
    "merge_tag": {
        "changeset": cs({"op": "merge_tag", "from": ["AI", "l'été"], "into": "topic:ai"})
    },
    "merge_tag_into_held_tag": {
        "changeset": cs({"op": "merge_tag", "from": ["unread"], "into": "status:to-read"})
    },
    "add_tag": {
        "changeset": cs({"op": "add_tag", "tag": "topic:ml", "keys": ["JOUR0001", "JOUR0002"]})
    },
    "add_tag_already_held": {
        "changeset": cs({"op": "add_tag", "tag": "status:to-read", "keys": ["JOUR0002"]})
    },
    "remove_tag": {
        "changeset": cs({"op": "remove_tag", "tag": "keep-me", "keys": ["JOUR0001", "JOUR0002"]})
    },
    "fill_field_empty_is_low_risk": {
        "changeset": cs({"op": "fill_field", "key": "JOUR0002", "field": "volume", "value": "12"})
    },
    "fill_field_overwrite_is_high_risk": {
        "changeset": cs({"op": "fill_field", "key": "JOUR0001", "field": "DOI", "value": "10.1/y"})
    },
    "fill_field_same_value_is_noop": {
        "changeset": cs({"op": "fill_field", "key": "JOUR0001", "field": "DOI", "value": "10.1/x"})
    },
    "clear_field": {"changeset": cs({"op": "clear_field", "key": "JOUR0001", "field": "DOI"})},
    "clear_field_already_empty": {
        "changeset": cs({"op": "clear_field", "key": "JOUR0002", "field": "volume"})
    },
    "set_creators_on_empty_is_low_risk": {
        "changeset": cs(
            {"op": "set_creators", "key": "JOUR0002", "creators": [creator(name="Goodfellow")]}
        )
    },
    "set_creators_replacing_is_high_risk": {
        "changeset": cs(
            {
                "op": "set_creators",
                "key": "JOUR0001",
                "creators": [
                    creator(firstName="A.", lastName="Vaswani"),
                    {"creatorType": "editor", "name": "Ed"},
                ],
            }
        )
    },
    "set_item_type_clears_foreign_fields": {
        "changeset": cs({"op": "set_item_type", "key": "BOOK0001", "itemType": "journalArticle"})
    },
    "set_item_type_same_type_is_noop": {
        "changeset": cs({"op": "set_item_type", "key": "BOOK0001", "itemType": "book"})
    },
    "set_item_type_webpage_to_book": {
        "changeset": cs({"op": "set_item_type", "key": "WEBP0001", "itemType": "book"})
    },
    "create_item_plain": {
        "changeset": cs(
            {
                "op": "create_item",
                "itemType": "book",
                "fields": {"title": "Wittgenstein: l'été", "date": "1983"},
                "creators": [creator(firstName="David", lastName="Bloor")],
                "tags": ["status:to-read", "topic:ai"],
                "collections": ["COLL0001"],
            }
        )
    },
    "create_item_minimal": {
        "changeset": cs({"op": "create_item", "itemType": "webpage", "fields": {"title": "T"}})
    },
    "create_item_adopts_attachment": {
        "changeset": cs(
            {
                "op": "create_item",
                "itemType": "journalArticle",
                "fields": {"title": "Scanned"},
                "tags": ["topic:ai", "topic:ml"],
                "collections": ["COLL0001", "COLL0002"],
                "attachment": "ATTC0001",
            }
        )
    },
    "create_item_adopts_bare_attachment": {
        "changeset": cs(
            {
                "op": "create_item",
                "itemType": "journalArticle",
                "fields": {"title": "Bare"},
                "attachment": "ATTC0002",
            }
        )
    },
    "add_to_collection": {
        "changeset": cs(
            {"op": "add_to_collection", "collection": "COLL0002", "keys": ["JOUR0001", "JOUR0002"]}
        )
    },
    "remove_from_collection": {
        "changeset": cs(
            {
                "op": "remove_from_collection",
                "collection": "COLL0001",
                "keys": ["JOUR0001", "JOUR0002"],
            }
        )
    },
    "create_collection": {
        "changeset": cs(
            {"op": "create_collection", "name": "Archive"},
            {"op": "create_collection", "name": "Sub", "parent": "COLL0001"},
            {"op": "create_collection", "name": "Root", "parent": None},
        )
    },
    "rename_collection": {
        "changeset": cs(
            {"op": "rename_collection", "collection": "COLL0002", "name": "Caixa d'entrada"},
            {"op": "rename_collection", "collection": "COLL0001", "name": "Projects"},
        )
    },
    "move_collection": {
        "changeset": cs(
            {"op": "move_collection", "collection": "COLL0003", "parent": None},
            {"op": "move_collection", "collection": "COLL0004", "parent": "COLL0002"},
            {"op": "move_collection", "collection": "COLL0002", "parent": None},
        )
    },
    "trash_collection": {
        "changeset": cs(
            {"op": "trash_collection", "collection": "COLL0004"},
            {"op": "trash_collection", "collection": "COLL0005"},
        )
    },
    "trash_item": {
        "changeset": cs(
            {"op": "trash_item", "key": "JOUR0002"}, {"op": "trash_item", "key": "JOUR0002"}
        )
    },
    "json_outcome": {
        "changeset": cs({"op": "add_tag", "tag": "topic:ml", "keys": ["JOUR0001"]}),
        "args": ["--json"],
    },
    # -- composition: later ops see earlier results --------------------------------
    "compose_ops_on_one_item": {
        "changeset": cs(
            {"op": "merge_tag", "from": ["AI"], "into": "topic:ai"},
            {"op": "add_tag", "tag": "rating:fav", "keys": ["JOUR0001"]},
            {"op": "fill_field", "key": "JOUR0001", "field": "volume", "value": "30"},
            {"op": "fill_field", "key": "JOUR0001", "field": "volume", "value": "31"},
            {"op": "remove_tag", "tag": "topic:ai", "keys": ["JOUR0001"]},
            {"op": "add_to_collection", "collection": "COLL0002", "keys": ["JOUR0001"]},
            {"op": "trash_item", "key": "JOUR0001"},
            slug="compose",
        )
    },
    "compose_new_collection_then_move_under_it": {
        "changeset": cs(
            {"op": "move_collection", "collection": "COLL0004", "parent": None},
            {"op": "move_collection", "collection": "COLL0001", "parent": "COLL0004"},
        )
    },
    "compose_on_objects_created_earlier": {
        "changeset": cs(
            {"op": "create_item", "itemType": "book", "fields": {"title": "New"}},
            {"op": "add_tag", "tag": "status:read", "keys": ["NEWC0001"]},
            {"op": "fill_field", "key": "NEWC0001", "field": "publisher", "value": "P"},
            {"op": "create_collection", "name": "Fresh"},
            {"op": "move_collection", "collection": "COLL0002", "parent": "NEWC0002"},
            {"op": "add_to_collection", "collection": "NEWC0002", "keys": ["NEWC0001"]},
        )
    },
    "compose_created_item_exclusivity": {
        "changeset": cs(
            {
                "op": "create_item",
                "itemType": "book",
                "fields": {"title": "New"},
                "tags": ["status:read"],
            },
            {"op": "add_tag", "tag": "status:to-read", "keys": ["NEWC0001"]},
        )
    },
    "retry_then_ok": {
        "changeset": cs({"op": "trash_item", "key": "JOUR0002"}),
        "fake": lambda: _scripted(
            validate_fake(), [httpx.Response(429, headers={"Retry-After": "0"})]
        ),
    },
    "golden": {
        "changeset": json.loads((GOLDEN_DIR / "changeset.json").read_text()),
        "fake": golden_fake,
        "registry": GOLDEN_REGISTRY,
        "backup": "20260719T115900Z",
        "instant": datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC),
        "golden": True,
    },
    # -- settings drift ---------------------------------------------------------
    "drift_when_colours_differ": {
        "changeset": cs({"op": "trash_item", "key": "JOUR0002"}),
        "fake": lambda: validate_fake([{"name": "ler", "color": "#FF0000"}]),
    },
    "drift_when_order_differs": {
        "changeset": cs({"op": "trash_item", "key": "JOUR0002"}),
        "fake": lambda: validate_fake(REGISTRY_COLOURS[::-1]),
        "args": ["--json"],
    },
    "no_drift_when_only_key_order_differs": {
        "changeset": cs({"op": "trash_item", "key": "JOUR0002"}),
        "fake": lambda: validate_fake(
            [{"color": c["color"], "name": c["name"]} for c in REGISTRY_COLOURS]
        ),
    },
    "drift_when_setting_unset": {
        "changeset": cs({"op": "trash_item", "key": "JOUR0002"}),
        "fake": lambda: validate_fake(None),
    },
    "no_registry_no_settings": {
        "changeset": cs({"op": "trash_item", "key": "JOUR0002"}),
        "fake": lambda: validate_fake([{"name": "ler", "color": "#FF0000"}]),
        "registry": None,
    },
    # -- semantic failures: every one collected, exit 2 ------------------------
    "fail_unknown_key": {
        "changeset": cs(
            {"op": "add_tag", "tag": "topic:ml", "keys": ["NOPE0001", "JOUR0001"]},
            {"op": "fill_field", "key": "NOPE0002", "field": "title", "value": "x"},
            {"op": "trash_item", "key": "TRSH0001"},
            {"op": "rename_collection", "collection": "NOPE0003", "name": "x"},
        )
    },
    "fail_alias_and_non_canonical": {
        "changeset": cs(
            {"op": "add_tag", "tag": "l'été", "keys": ["JOUR0001"]},
            {"op": "add_tag", "tag": "Misc", "keys": ["JOUR0001"]},
            {"op": "merge_tag", "from": ["x"], "into": "read"},
        )
    },
    "fail_merge_canonical_away": {
        "changeset": cs(
            {"op": "merge_tag", "from": ["topic:ml", "AI", "topic:ai"], "into": "topic:ai"}
        )
    },
    "fail_tags_without_registry": {
        "changeset": cs(
            {"op": "add_tag", "tag": "topic:ml", "keys": ["JOUR0001"]},
            {"op": "merge_tag", "from": ["AI"], "into": "topic:ai"},
            {"op": "remove_tag", "tag": "AI", "keys": ["JOUR0001"]},
        ),
        "registry": None,
    },
    "fail_extra_field": {
        "changeset": cs(
            {"op": "fill_field", "key": "JOUR0001", "field": "extra", "value": "x"},
            {"op": "clear_field", "key": "JOUR0001", "field": "extra"},
            {"op": "create_item", "itemType": "book", "fields": {"extra": "x", "title": "t"}},
        )
    },
    "fail_invalid_field": {
        "changeset": cs(
            {"op": "fill_field", "key": "JOUR0001", "field": "publisher", "value": "x"},
            {"op": "create_item", "itemType": "book", "fields": {"volume": "1", "DOI": "d"}},
        )
    },
    "fail_invalid_creator_type": {
        "changeset": cs(
            {
                "op": "set_creators",
                "key": "JOUR0001",
                "creators": [
                    {"creatorType": "composer", "name": "a"},
                    {"creatorType": "x", "name": "b"},
                ],
            },
            {
                "op": "create_item",
                "itemType": "webpage",
                "fields": {"title": "t"},
                "creators": [
                    {"creatorType": "editor", "name": "a"},
                    {"creatorType": "x", "name": "b"},
                ],
            },
        )
    },
    "fail_invalid_item_type": {
        "changeset": cs(
            {"op": "create_item", "itemType": "spaceship", "fields": {"title": "t"}},
            {"op": "set_item_type", "key": "JOUR0001", "itemType": "spaceship"},
        )
    },
    "fail_set_item_type_creator_mismatch": {
        "changeset": cs({"op": "set_item_type", "key": "BOOK0001", "itemType": "webpage"})
    },
    "fail_exclusivity": {
        "changeset": cs(
            {"op": "add_tag", "tag": "status:read", "keys": ["JOUR0002", "JOUR0001"]},
            {"op": "add_tag", "tag": "rating:fav", "keys": ["JOUR0001"]},
            {
                "op": "create_item",
                "itemType": "book",
                "fields": {"title": "t"},
                "tags": ["status:read", "status:to-read", "rating:fav"],
            },
        )
    },
    "stock_exclusivity_violation_is_not_a_veto": {
        "changeset": cs(
            {"op": "add_tag", "tag": "topic:ml", "keys": ["DUPL0001"]},
            {"op": "remove_tag", "tag": "status:read", "keys": ["DUPL0001"]},
        )
    },
    "fail_exclusivity_on_merge": {
        "changeset": cs(
            {"op": "merge_tag", "from": ["keep-me"], "into": "status:read"},
            {"op": "add_tag", "tag": "status:to-read", "keys": ["JOUR0001"]},
        )
    },
    "fail_missing_collections": {
        "changeset": cs(
            {"op": "add_to_collection", "collection": "NOPE0001", "keys": ["JOUR0001"]},
            {
                "op": "create_item",
                "itemType": "book",
                "fields": {"title": "t"},
                "collections": ["NOPE0002", "COLL0001", "NOPE0003"],
            },
            {"op": "create_collection", "name": "x", "parent": "NOPE0004"},
            {"op": "move_collection", "collection": "COLL0001", "parent": "NOPE0005"},
            {"op": "trash_collection", "collection": "NOPE0006"},
        )
    },
    "fail_move_would_cycle": {
        "changeset": cs(
            {"op": "move_collection", "collection": "COLL0001", "parent": "COLL0004"},
            {"op": "move_collection", "collection": "COLL0003", "parent": "COLL0003"},
        )
    },
    "fail_attachment_not_adoptable": {
        "changeset": cs(
            {
                "op": "create_item",
                "itemType": "book",
                "fields": {"title": "a"},
                "attachment": "JOUR0001",
            },
            {
                "op": "create_item",
                "itemType": "book",
                "fields": {"title": "b"},
                "attachment": "ATTC0003",
            },
            {
                "op": "create_item",
                "itemType": "book",
                "fields": {"title": "c"},
                "attachment": "NOPE0001",
            },
        )
    },
    "fail_json_outcome": {
        "changeset": cs({"op": "add_tag", "tag": "AI", "keys": ["NOPE0001"]}),
        "args": ["--json"],
    },
    "fail_schema_lookup_for_unknown_stored_type": {
        "changeset": cs({"op": "fill_field", "key": "NOTE0001", "field": "title", "value": "x"})
    },
    # -- structural lint: exit 2 before any request ---------------------------------
    "lint_missing_file": {"changeset": None},
    "lint_not_json_empty": {"changeset": ""},
    "lint_not_json_trailing_comma": {"changeset": '{"schema": "changeset.v1",\n  "intents": [1,]}'},
    "lint_not_json_object_comma": {"changeset": '{"a": 1,}'},
    "lint_not_json_missing_colon": {"changeset": '{"a" 1}'},
    "lint_not_json_missing_comma": {"changeset": '{"a": [1 2]}'},
    "lint_not_json_property_name": {"changeset": "{\n\t'a': 1}"},
    "lint_not_json_extra_data": {"changeset": '{"a": 1}\r\n\r\n  x'},
    "lint_not_json_bad_escape": {"changeset": '{"é": "a\\qb"}'},
    "lint_not_json_bad_unicode_escape": {"changeset": '["\\u12zz"]'},
    "lint_not_json_short_unicode_escape": {"changeset": '"\\u12"'},
    "lint_not_json_bad_surrogate_pair": {"changeset": '["\\ud800\\uzzzz"]'},
    "lint_not_json_unterminated": {"changeset": '{"a": "abc'},
    "lint_not_json_control_char": {"changeset": '{"a": "a\tb"}'},
    "lint_not_json_unclosed": {"changeset": '{"a": ['},
    "lint_not_json_bom": {"changeset": "﻿{}"},
    "lint_not_json_bad_literal": {"changeset": "[nul]"},
    "lint_not_json_number": {"changeset": "[-]"},
    "lint_not_an_object": {"changeset": "[1, 2]"},
    "lint_schema_missing": {"changeset": {"slug": "x", "intents": []}},
    "lint_schema_wrong": {"changeset": {"schema": {"v": 1.0, "ok": True, "n": None}}},
    "lint_slug_missing": {"changeset": {"schema": "changeset.v1"}},
    "lint_slug_not_kebab": {"changeset": {"schema": "changeset.v1", "slug": "Bad Slug"}},
    "lint_slug_number": {"changeset": {"schema": "changeset.v1", "slug": 12}},
    "lint_slug_leading_dash": {"changeset": {"schema": "changeset.v1", "slug": "-x"}},
    "lint_slug_trailing_newline_passes": {
        "changeset": cs({"op": "trash_item", "key": "JOUR0002"}, slug="nl\n")
    },
    "lint_slug_two_newlines": {"changeset": {"schema": "changeset.v1", "slug": "nl\n\n"}},
    "lint_intents_missing": {"changeset": {"schema": "changeset.v1", "slug": "x"}},
    "lint_intents_empty": {"changeset": cs()},
    "lint_intents_not_list": {"changeset": cs(slug="x") | {"intents": {"op": "trash_item"}}},
    "lint_intent_not_mapping": {"changeset": cs(["trash_item"])},
    "lint_unknown_op": {"changeset": cs({"op": "delete_item", "key": "JOUR0001"})},
    "lint_op_missing": {"changeset": cs({"key": "JOUR0001"})},
    "lint_unknown_fields": {
        "changeset": cs({"op": "trash_item", "key": "K", "why": 1, "because": 2})
    },
    "lint_missing_field": {"changeset": cs({"op": "merge_tag", "into": "topic:ai"})},
    "lint_str_empty": {"changeset": cs({"op": "trash_item", "key": ""})},
    "lint_str_number": {"changeset": cs({"op": "trash_item", "key": 1.5e-7})},
    "lint_str_null": {"changeset": cs({"op": "trash_item", "key": None})},
    "lint_str_bool": {"changeset": cs({"op": "trash_item", "key": False})},
    "lint_str_or_null_empty": {
        "changeset": cs({"op": "move_collection", "collection": "C", "parent": ""})
    },
    "lint_str_or_null_missing": {"changeset": cs({"op": "move_collection", "collection": "C"})},
    "lint_str_or_null_list": {
        "changeset": cs({"op": "create_collection", "name": "n", "parent": ["a"]})
    },
    "lint_str_list_empty": {"changeset": cs({"op": "add_tag", "tag": "t", "keys": []})},
    "lint_str_list_blank": {"changeset": cs({"op": "add_tag", "tag": "t", "keys": ["a", ""]})},
    "lint_str_list_numbers": {
        "changeset": cs({"op": "add_tag", "tag": "t", "keys": [1, 100000.0]})
    },
    "lint_str_list_string": {"changeset": cs({"op": "merge_tag", "from": "AI", "into": "t"})},
    "lint_optional_str_list": {
        "changeset": cs(
            {"op": "create_item", "itemType": "book", "fields": {"t": "x"}, "tags": [None]}
        )
    },
    "lint_str_map_empty": {
        "changeset": cs({"op": "create_item", "itemType": "book", "fields": {}})
    },
    "lint_str_map_blank_value": {
        "changeset": cs({"op": "create_item", "itemType": "book", "fields": {"title": ""}})
    },
    "lint_str_map_nested": {
        "changeset": cs(
            {"op": "create_item", "itemType": "book", "fields": {"title": {"a": [True]}}}
        )
    },
    "lint_str_map_list": {
        "changeset": cs({"op": "create_item", "itemType": "book", "fields": ["title"]})
    },
    "lint_optional_str": {
        "changeset": cs(
            {"op": "create_item", "itemType": "book", "fields": {"t": "x"}, "attachment": 0}
        )
    },
    "lint_creators_empty": {"changeset": cs({"op": "set_creators", "key": "K", "creators": []})},
    "lint_creators_not_list": {
        "changeset": cs({"op": "set_creators", "key": "K", "creators": {"name": "x"}})
    },
    "lint_creator_not_mapping": {
        "changeset": cs({"op": "set_creators", "key": "K", "creators": ["rui's"]})
    },
    "lint_creator_unknown_keys": {
        "changeset": cs(
            {"op": "set_creators", "key": "K", "creators": [creator(name="a", lastname="b", z=1)]}
        )
    },
    "lint_creator_no_type": {
        "changeset": cs({"op": "set_creators", "key": "K", "creators": [{"name": "a"}]})
    },
    "lint_creator_empty_type": {
        "changeset": cs(
            {"op": "set_creators", "key": "K", "creators": [{"creatorType": "", "name": "a"}]}
        )
    },
    "lint_creator_both_forms": {
        "changeset": cs(
            {"op": "set_creators", "key": "K", "creators": [creator(name="a", lastName="b")]}
        )
    },
    "lint_creator_neither_form": {
        "changeset": cs({"op": "set_creators", "key": "K", "creators": [creator()]})
    },
    "lint_creator_empty_name": {
        "changeset": cs(
            {
                "op": "create_item",
                "itemType": "book",
                "fields": {"t": "x"},
                "creators": [creator(firstName="", lastName="b")],
            }
        )
    },
    "lint_first_bad_intent_wins": {
        "changeset": cs({"op": "trash_item", "key": "K"}, {"op": "trash_item"}, {"op": "nope"})
    },
    "lint_path_normalised": {"changeset": None, "path": "changesets//./absent.json"},
    # -- the registry and the backup gate: exit 1 -----------------------------------
    "no_backup": {"changeset": cs({"op": "trash_item", "key": "JOUR0002"}), "backup": None},
}


TRIVIAL = cs({"op": "trash_item", "key": "JOUR0002"})
FAMILIES = "families:\n  status: {coloured: true, exclusive: true}\n  topic: {}\n"

# Defective registries: validate stops before any request, exit 1, as audit does.
REGISTRY_CASES = {
    "not_a_mapping": "- a\n- b\n",
    "empty_file": "",
    "families_not_mapping": "families: [status]\ntags: []\n",
    "tags_missing": "families: {}\n",
    "tags_not_list": "families: {}\ntags: {a: 1}\n",
    "family_not_mapping": "families:\n  status: [1, 2.5, true, null]\ntags: []\n",
    "family_unknown_keys": "families:\n  status: {color: true, colored: x}\ntags: []\n",
    "family_numeric_name": "families:\n  12: oops\ntags: []\n",
    "tag_entry_not_mapping": FAMILIES + "tags:\n  - status:read\n",
    "tag_entry_unknown_keys": FAMILIES + "tags:\n  - {tag: status:read, color: '#E69F00'}\n",
    "tag_not_family_value": FAMILIES + "tags:\n  - tag: Status:Read\n",
    "tag_missing": FAMILIES + "tags:\n  - {description: d}\n",
    "tag_number": FAMILIES + "tags:\n  - {tag: 12}\n",
    "tag_trailing_newline_passes": FAMILIES + 'tags:\n  - {tag: "topic:x\\n"}\n',
    "undeclared_family": FAMILIES + "tags:\n  - tag: rating:fav\n",
    "duplicate_canonical": FAMILIES + "tags:\n  - tag: topic:ai\n  - tag: topic:ai\n",
    "alias_shadows_canonical": FAMILIES
    + "tags:\n  - tag: topic:ai\n  - {tag: topic:ml, aliases: [topic:ai]}\n",
    "alias_two_owners": FAMILIES
    + "tags:\n  - {tag: topic:ai, aliases: [done]}\n  - {tag: topic:ml, aliases: [done]}\n",
    "aliases_as_a_string": FAMILIES
    + "tags:\n  - {tag: topic:ai, aliases: ab}\n  - {tag: topic:ml, aliases: [b]}\n",
    "colour_not_hex": FAMILIES + "tags:\n  - {tag: status:read, colour: 'E69F00'}\n",
    "colour_too_long": FAMILIES + "tags:\n  - {tag: status:read, colour: '#E69F00A'}\n",
    "colour_illegible": FAMILIES + "tags:\n  - {tag: status:read, colour: '#f0e442'}\n",
    "colour_on_uncoloured_family": FAMILIES + "tags:\n  - {tag: topic:ai, colour: '#E69F00'}\n",
    "too_many_coloured": FAMILIES
    + "tags:\n"
    + "".join(f"  - {{tag: 'status:s{i}', colour: '#E69F00'}}\n" for i in range(10)),
    "falsy_colour_is_uncoloured": FAMILIES + "tags:\n  - {tag: topic:ai, colour: ''}\n",
    "family_flags_truthy": "families:\n  status: {coloured: 1, exclusive: yes please}\n"
    + "  topic:\ntags:\n  - {tag: status:read, colour: '#009E73', description: 3}\n",
}
for _name, _text in REGISTRY_CASES.items():
    VALIDATE_CASES[f"registry_{_name}"] = {"changeset": TRIVIAL, "registry": _text}
    VALIDATE_CASES[f"registry_{_name}_audit"] = {
        "changeset": TRIVIAL,
        "registry": _text,
        "command": ["audit", "tags"],
    }


def seed_backup(root: Path, timestamp: str | None) -> None:
    """The newest backup validate pins its plan to, or none at all."""
    root.mkdir(parents=True)
    if timestamp is not None:
        (root / "backups").mkdir()
        (root / "backups" / f"{timestamp}.jsonl").write_text(
            json.dumps({"kind": "header", "library_version": 1, "timestamp": timestamp}) + "\n"
        )


def write_changeset_file(root: Path, name: str, changeset) -> None:
    if changeset is None:
        return
    text = changeset if isinstance(changeset, str) else json.dumps(changeset, indent=2)
    (root / "changesets").mkdir()
    # Bytes, so a test's \r\n reaches the file as written.
    (root / "changesets" / f"{name}.json").write_bytes(text.encode())


@pytest.mark.parametrize("name", sorted(VALIDATE_CASES))
def test_rust_validate_matches_python(
    rust_bin, tmp_path, monkeypatch, isolated_config_dir, test_tz, name
):
    case = VALIDATE_CASES[name]
    registry = case.get("registry", REGISTRY)
    if registry is not None:
        isolated_config_dir.mkdir(parents=True)
        (isolated_config_dir / "taxonomy.yaml").write_text(registry)
    instant = case.get("instant", INSTANTS["default"])
    results, files = [], []
    for side in ("py", "rs"):
        data = tmp_path / side / "data"
        seed_backup(data, case.get("backup", "20261101T120000Z"))
        write_changeset_file(data, name, case["changeset"])
        # Joined as text, not with pathlib, so an unnormalised path reaches the CLI.
        target = f"{data}/{case.get('path', f'changesets/{name}.json')}"
        args = case.get("command") or ["validate", target, *case.get("args", [])]
        fake = case.get("fake", validate_fake)()
        if side == "py":
            outcome = run_python(args, fake, data, monkeypatch, instant)
        else:
            outcome = run_rust(rust_bin, args, fake, data, instant)
        results.append((normalise(outcome, data), request_log(fake)))
        files.append(snapshot(data))
    (expected, expected_requests), (actual, actual_requests) = results
    assert actual == expected
    assert files[1] == files[0]
    assert actual_requests == expected_requests
    if case.get("golden"):
        assert (
            files[1]["plans/20260719T120000Z-golden-sweep.json"]
            == (GOLDEN_DIR / "plan.json").read_text()
        )


XDG_VALUES = {"absolute": "<tmp>/xdg", "relative": "rel", "padded": "  <tmp>/xdg  ", "blank": "  "}


@pytest.mark.parametrize("xdg", [*XDG_VALUES, "unset"])
def test_default_dirs_resolve_like_platformdirs(rust_bin, tmp_path, monkeypatch, xdg):
    """With no ZELADOR_* override, Rust must land on platformdirs' config and data
    dirs: XDG values are stripped, relative ones accepted, blank ones ignored."""
    env = {"HOME": str(tmp_path / "home")}
    if xdg != "unset":
        value = XDG_VALUES[xdg].replace("<tmp>", str(tmp_path))
        # The same shape for data, so padding reaches both ends of both variables.
        data_value = XDG_VALUES[xdg].replace("<tmp>/xdg", f"{tmp_path}/xdg-data")
        data_value = data_value.replace("rel", "rel-data")
        env |= {"XDG_CONFIG_HOME": value, "XDG_DATA_HOME": data_value}
    monkeypatch.chdir(tmp_path)
    for name in ("ZELADOR_CONFIG_DIR", "ZELADOR_DATA_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    expected_config, expected_data = config.config_dir(), config.data_dir()
    expected_config.mkdir(parents=True)
    (expected_config / "config.yaml").write_text("style: apa\n")

    bridge = Bridge(library_fake())
    try:
        proc = subprocess.run(
            [str(rust_bin), "status", "--json"],
            env={
                "PATH": os.environ["PATH"],
                "ZELADOR_API_BASE": bridge.origin,
                "ZOTERO_API_KEY": API_KEY,
                "ZOTERO_USER_ID": USER_ID,
                **env,
            },
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        bridge.close()
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["config"]["config_yaml"] is True
    assert (tmp_path / expected_data / "backups").is_dir()
