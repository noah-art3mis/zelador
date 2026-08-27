"""Tests for the shared current-state helpers — reading a live facet for comparison."""

from zelador.write.library_state import live_value


class TestLiveValue:
    def test_absent_set_valued_fields_read_as_empty(self):
        # Zotero omits `collections` on child items and may return a set-valued
        # field as null. Comparison has to see [] rather than None: state_equal
        # sorts these, so a None would raise instead of reporting a mismatch.
        assert live_value({}, "collections") == []
        assert live_value({"collections": None}, "collections") == []
        assert live_value({}, "tags") == []
        assert live_value({}, "creators") == []

    def test_absent_parent_pointers_read_as_false(self):
        # False is Zotero's "no parent", distinct from a missing key.
        assert live_value({}, "parentItem") is False
        assert live_value({}, "parentCollection") is False

    def test_deleted_reads_as_a_bool(self):
        assert live_value({}, "deleted") is False
        assert live_value({"deleted": 1}, "deleted") is True

    def test_other_fields_read_straight_through(self):
        assert live_value({"title": "A title"}, "title") == "A title"
        assert live_value({}, "title") is None
