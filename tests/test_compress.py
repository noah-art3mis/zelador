"""The deterministic core of `zel compress`: selection, verdicts, contract, move preconditions."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from zelador.compress import (
    Candidate,
    CompressError,
    Entry,
    Facts,
    Report,
    accepted,
    allocate_run_id,
    report_from_dict,
    report_to_dict,
    restore_blockers,
    select_candidates,
    swap_blockers,
    verdict,
)


def facts(size, pages=10, text_len=1000, annots=0):
    return Facts(bytes=size, pages=pages, text_len=text_len, annots=annots)


def entry(key="AAA", ok=True, size=1000, original="orig-md5", staged="staged-md5", name=None):
    return Entry(
        key=key,
        filename=name or f"{key}.pdf",
        accepted=ok,
        reason="saved 50.0%",
        before=facts(size),
        after=facts(size // 2),
        original_md5=original,
        staged_md5=staged,
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

    def test_two_files_under_one_key_are_two_candidates(self):
        """A storage folder can hold more than one payload — the key is not an identity."""
        files = [
            Candidate(key="AAA", filename="b.pdf", bytes=200),
            Candidate(key="AAA", filename="a.pdf", bytes=200),
        ]
        picked = select_candidates(files, min_bytes=0, limit=None)
        assert [c.id for c in picked] == [("AAA", "a.pdf"), ("AAA", "b.pdf")]


class TestVerdict:
    def test_accepts_a_real_saving(self):
        ok, reason = verdict(facts(1000), facts(250), min_saving=0.25, text_tolerance=0.02)
        assert ok and "75.0%" in reason

    def test_rejects_a_changed_page_count(self):
        ok, reason = verdict(
            facts(1000, pages=613), facts(100, pages=612), min_saving=0.25, text_tolerance=0.02
        )
        assert not ok and "613 -> 612" in reason

    def test_rejects_lost_annotations(self):
        """Embedded highlights live in the file; a compressor that flattens them
        produces a smaller, same-paged, same-texted file that has eaten the markup."""
        ok, reason = verdict(
            facts(1000, annots=132), facts(200, annots=0), min_saving=0.25, text_tolerance=0.02
        )
        assert not ok and "annotations lost: 132 -> 0" in reason

    def test_gained_annotations_are_not_a_rejection(self):
        ok, _ = verdict(
            facts(1000, annots=3), facts(200, annots=4), min_saving=0.25, text_tolerance=0.02
        )
        assert ok

    def test_rejects_a_collapsed_text_layer(self):
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
        entries = [entry()]
        ids = {("AAA", "AAA.pdf")}
        assert (
            swap_blockers(
                entries,
                dict.fromkeys(ids, "orig-md5"),
                dict.fromkeys(ids, "staged-md5"),
            )
            == []
        )

    def test_storage_file_changed_since_scan(self):
        """Someone re-annotated the PDF after the scan — the staged copy is stale."""
        blockers = swap_blockers(
            [entry()], {("AAA", "AAA.pdf"): "different"}, {("AAA", "AAA.pdf"): "staged-md5"}
        )
        assert len(blockers) == 1 and "changed" in blockers[0]

    def test_missing_storage_file(self):
        blockers = swap_blockers([entry()], {}, {("AAA", "AAA.pdf"): "staged-md5"})
        assert len(blockers) == 1 and "no such file" in blockers[0]

    def test_missing_staged_file(self):
        blockers = swap_blockers([entry()], {("AAA", "AAA.pdf"): "orig-md5"}, {})
        assert len(blockers) == 1 and "missing" in blockers[0]

    def test_half_written_staged_file_is_refused(self):
        """It exists, so presence proves nothing — only its content does."""
        blockers = swap_blockers(
            [entry()], {("AAA", "AAA.pdf"): "orig-md5"}, {("AAA", "AAA.pdf"): "truncated"}
        )
        assert len(blockers) == 1 and "damaged" in blockers[0]

    def test_entries_are_identified_by_key_and_filename(self):
        """Two PDFs in one storage folder must not shadow each other's hashes."""
        entries = [entry(key="AAA", name="a.pdf"), entry(key="AAA", name="b.pdf")]
        storage = {("AAA", "a.pdf"): "orig-md5", ("AAA", "b.pdf"): "orig-md5"}
        staged = {("AAA", "a.pdf"): "staged-md5", ("AAA", "b.pdf"): "staged-md5"}
        assert swap_blockers(entries, storage, staged) == []


class TestRestoreBlockers:
    def test_clean_swap_can_be_restored(self):
        ids = {("AAA", "AAA.pdf")}
        assert (
            restore_blockers(
                [entry()], dict.fromkeys(ids, "staged-md5"), dict.fromkeys(ids, "orig-md5")
            )
            == []
        )

    def test_partial_quarantine_file_is_refused(self):
        """A move interrupted mid-copy leaves a real file holding partial bytes;
        restoring it would overwrite an intact library file with a fragment."""
        blockers = restore_blockers(
            [entry()], {("AAA", "AAA.pdf"): "staged-md5"}, {("AAA", "AAA.pdf"): "half"}
        )
        assert len(blockers) == 1 and "damaged" in blockers[0]

    def test_missing_storage_file_is_not_a_blocker(self):
        """A swap that died between its two moves — restoring is the repair."""
        assert restore_blockers([entry()], {}, {("AAA", "AAA.pdf"): "orig-md5"}) == []

    def test_another_run_swapped_it_since(self):
        """Restoring now would install this run's original over a later run's work,
        and hand that later run's compressed file back as if it were an original."""
        blockers = restore_blockers(
            [entry()],
            {("AAA", "AAA.pdf"): "some-other-runs-compressed-file"},
            {("AAA", "AAA.pdf"): "orig-md5"},
        )
        assert len(blockers) == 1 and "another run" in blockers[0]

    def test_nothing_quarantined(self):
        blockers = restore_blockers([entry()], {("AAA", "AAA.pdf"): "staged-md5"}, {})
        assert len(blockers) == 1 and "no quarantined original" in blockers[0]


class TestAllocateRunId:
    def test_is_a_sortable_utc_stamp(self):
        assert (
            allocate_run_id(datetime(2026, 8, 7, 10, 15, 0, tzinfo=UTC), set())
            == "20260807T101500Z-compress"
        )

    def test_second_run_in_the_same_second_gets_its_own_directory(self):
        """Sharing one would let the second report orphan the first run's originals."""
        now = datetime(2026, 8, 7, 10, 15, 0, tzinfo=UTC)
        taken = {"20260807T101500Z-compress"}
        assert allocate_run_id(now, taken) == "20260807T101500Z-compress-2"
        taken.add("20260807T101500Z-compress-2")
        assert allocate_run_id(now, taken) == "20260807T101500Z-compress-3"
