"""Telegram message helpers — markdown escaping and (phase 5) sender utilities.

Phase 3 has just `escape_md_v2`. Phase 5 will add the actual `send_message` /
`send_post_run_review` / `send_next_session` helpers that the coaching pipeline
uses.

See [spec.md §11.19](spec.md) for the MarkdownV2 escaping convention.
"""

from __future__ import annotations

# MarkdownV2 special characters that must be escaped in user-visible text per
# the Telegram Bot API. Source:
# https://core.telegram.org/bots/api#markdownv2-style
_MD_V2_SPECIAL = set(r"_*[]()~`>#+-=|{}.!")


def escape_md_v2(text: str) -> str:
    """Escape every MarkdownV2 special character in `text` with a backslash.

    Use on ALL dynamic content before interpolating it into a MarkdownV2
    message template. Constant strings in templates (the parts you author)
    are escaped once at write time.

    Examples:
        escape_md_v2("8.0km")        -> '8\\.0km'
        escape_md_v2("(45:00)")      -> '\\(45:00\\)'
        escape_md_v2("4-5K race")    -> '4\\-5K race'
        escape_md_v2("hello world")  -> 'hello world'  (unchanged)

    NOT idempotent: escaping twice doubles the backslashes.
    """
    return "".join("\\" + c if c in _MD_V2_SPECIAL else c for c in text)
