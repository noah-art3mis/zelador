"""The deterministic core of `zel compress`: selection, verdicts, contract, swap preconditions."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from zelador.compress import (
    CONTRACT,
    Candidate,
    CompressError,
    Entry,
    Facts,
    Report,
    accepted,
    report_from_dict,
    report_to_dict,
    run_id,
    select_candidates,
    swap_blockers,
    verdict,
)


def facts(size, pages=10, text_len=1000):
    return Facts(bytes=size, pages=pages, text_len=text_len)


def entry(key="AAA", ok=True, size=1000, md5="abc"):
    return Entry(
        key=key,
        filename=f"{key}.pdf",
        accepted=ok,
        reason="saved 50.0%",
        before=facts(size),
        after=facts(size // 2),
        md5=md5,
    )


def report(entries=None, run="20260807T101500Z-compress"):
    return Report(
        run=run,
        created="2026-08-07T10:15:00+00:00",
        zotero_dir="/z",
        settings={"preset": "ebook", "min_saving": 0.25},
        entries=entries if entries is not None else [entry()],
    )


class TestSelectCandidates:
    def test_only_pdfs_over_the_floor_largest_first(self):
        files = [
            Candidate(key="SMALL", filename="a.pdf", bytes=1000),
            Candidate(key="BIG", filename="b.pdf", bytes=9000),
            Candidate(key="MID", filename="c.pdf", bytes=5000),
            Candidate(key="HTML", filename="page.html", bytes=9999),
        ]
        picked = select_candidates(files, min_bytes=5000, limit=None)
        assert [c.key for c in picked] == ["BIG", "MID"]

    def test_limit_keeps_the_largest(self):
        files = [Candidate(key=f"K{i}", filename="f.pdf", bytes=i * 100) for i in range(1, 6)]
        assert [c.key for c in select_candidates(files, min_bytes=0, limit=2)] == ["K5", "K4"]

    def test_uppercase_extension_is_still_a_pdf(self):
        files = [Candidate(key="SHOUT", filename="SCAN.PDF", bytes=9000)]
        assert [c.key for c in select_candidates(files, min_bytes=0, limit=None)] == ["SHOUT"]


class TestVerdict:
    def test_accepts_a_real_saving(self):
        ok, reason = verdict(facts(1000), facts(250), min_saving=0.25, text_tolerance=0.02)
        assert ok and "75.0%" in reason

    def test_rejects_a_changed_page_count(self):
        ok, reason = verdict(
            facts(1000, pages=613), facts(100, pages=612), min_saving=0.25, text_tolerance=0.02
        )
        assert not ok and "613 -> 612" in reason

    def test_rejects_a_collapsed_text_layer(self):
        """A 99% saving that ate the text layer is a corrupted file, not a win."""
        ok, reason = verdict(
            facts(1000, text_len=50000),
            facts(10, text_len=12),
            min_saving=0.25,
            text_tolerance=0.02,
        )
        assert not ok and "text layer" in reason

    def test_text_within_tolerance_survives(self):
        ok, _ = verdict(
            facts(1000, text_len=10000),
            facts(400, text_len=9900),
            min_saving=0.25,
            text_tolerance=0.02,
        )
        assert ok

    def test_rejects_a_saving_below_the_floor(self):
        ok, reason = verdict(facts(1000), facts(900), min_saving=0.25, text_tolerance=0.02)
        assert not ok and "10.0%" in reason

    def test_rejects_a_file_that_grew(self):
        ok, reason = verdict(facts(1000), facts(1400), min_saving=0.25, text_tolerance=0.02)
        assert not ok and "-40.0%" in reason

    def test_rejects_an_empty_original_without_dividing_by_zero(self):
        ok, reason = verdict(facts(0), facts(0), min_saving=0.25, text_tolerance=0.02)
        assert not ok and "empty" in reason

    def test_page_count_beats_saving_in_the_reason(self):
        """Both rules fail; the alarming one is what the operator must read."""
        ok, reason = verdict(
            facts(1000, pages=10), facts(990, pages=9), min_saving=0.25, text_tolerance=0.02
        )
        assert not ok and "pages" in reason


class TestContract:
    def test_round_trip_preserves_entries(self):
        original = report([entry(key="AAA"), entry(key="BBB", ok=False)])
        restored = report_from_dict(report_to_dict(original))
        assert restored == original

    def test_serialized_form_is_stamped(self):
        assert report_to_dict(report())["contract"] == CONTRACT

    def test_foreign_contract_is_refused(self):
        raw = report_to_dict(report())
        raw["contract"] = "compress.v99"
        with pytest.raises(CompressError, match="compress.v99"):
            report_from_dict(raw)

    def test_accepted_filters_rejections(self):
        rep = report([entry(key="YES"), entry(key="NO", ok=False)])
        assert [e.key for e in accepted(rep)] == ["YES"]


class TestSwapBlockers:
    def test_clean_run_has_none(self):
        rep = report([entry(key="AAA", md5="abc")])
        assert swap_blockers(rep, {"AAA": "abc"}, {"AAA": True}) == []

    def test_storage_file_changed_since_scan(self):
        """Someone re-annotated the PDF after the scan — the staged copy is stale."""
        rep = report([entry(key="AAA", md5="abc")])
        blockers = swap_blockers(rep, {"AAA": "different"}, {"AAA": True})
        assert len(blockers) == 1 and "AAA" in blockers[0] and "changed" in blockers[0]

    def test_missing_storage_file(self):
        rep = report([entry(key="AAA", md5="abc")])
        blockers = swap_blockers(rep, {}, {"AAA": True})
        assert len(blockers) == 1 and "AAA" in blockers[0]

    def test_missing_staged_file(self):
        rep = report([entry(key="AAA", md5="abc")])
        blockers = swap_blockers(rep, {"AAA": "abc"}, {"AAA": False})
        assert len(blockers) == 1 and "staged" in blockers[0]

    def test_rejected_entries_are_not_blockers(self):
        """A rejected candidate is never swapped, so its md5 drifting is irrelevant."""
        rep = report([entry(key="NO", ok=False, md5="abc")])
        assert swap_blockers(rep, {"NO": "moved-on"}, {"NO": False}) == []


class TestRunId:
    def test_is_a_sortable_utc_stamp(self):
        assert run_id(datetime(2026, 8, 7, 10, 15, 0, tzinfo=UTC)) == "20260807T101500Z-compress"
