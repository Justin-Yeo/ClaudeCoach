"""Tests for `app.services.telegram.escape_md_v2`."""

from __future__ import annotations

import pytest

from app.services.telegram import escape_md_v2


class TestEscapeMdV2:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("8.0km", "8\\.0km"),
            ("(target 45:00)", "\\(target 45:00\\)"),
            ("4-5K race", "4\\-5K race"),
            ("hello world", "hello world"),  # nothing to escape
            ("", ""),
            ("a*b", "a\\*b"),
            ("foo_bar", "foo\\_bar"),
            ("Sun 12 Apr 2026", "Sun 12 Apr 2026"),  # spaces and digits unaffected
            ("4:30/km", "4:30/km"),  # colon and slash unaffected
            ("e.g.", "e\\.g\\."),
            ("[link]", "\\[link\\]"),
        ],
    )
    def test_escape(self, raw, expected):
        assert escape_md_v2(raw) == expected

    def test_all_special_chars_individually(self):
        for c in r"_*[]()~`>#+-=|{}.!":
            assert escape_md_v2(c) == "\\" + c, f"failed for {c!r}"

    def test_double_escape_is_not_idempotent(self):
        # Backslashes themselves aren't in the special-char list, so they pass
        # through. But the period gets re-escaped on every pass, so the output
        # grows by one char per call.
        once = escape_md_v2("a.b")
        twice = escape_md_v2(once)
        assert twice != once
        assert len(twice) == len(once) + 1

    def test_long_message(self):
        # Sanity check on a realistic message
        msg = "Run logged. 8.0km in 45:12 (avg 5:39/km)."
        out = escape_md_v2(msg)
        assert "8\\.0km" in out
        assert "\\(avg 5:39/km\\)\\." in out
