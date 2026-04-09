"""Prompt v1 loader.

Parses `app/prompts/v1.md` into:
- `SYSTEM_PROMPT`: Claude's persona and coaching rules (stable across all users)
- `USER_TEMPLATE`: per-call data payload with `{placeholders}` filled by the
  prompt builder in `app/services/claude.py`

Also loads the `submit_coaching` tool schema from
`coaching_tool_schema.json` into `COACHING_TOOL`.

`PROMPT_VERSION` is the string stored in `runs.prompt_version` on every
coaching call — see [spec.md §11.6](spec.md). Bump this string whenever the
prompt or tool schema changes meaningfully so you can correlate response
quality with iterations later.
"""

from __future__ import annotations

import json
from pathlib import Path

PROMPT_VERSION = "v1"

_THIS_DIR = Path(__file__).parent
_V1_MD_PATH = _THIS_DIR / "v1.md"
_TOOL_SCHEMA_PATH = _THIS_DIR / "coaching_tool_schema.json"

_SYS_HEADER = "## SYSTEM PROMPT"
_USER_HEADER = "## USER PROMPT TEMPLATE"


def _load_prompts() -> tuple[str, str]:
    """Parse v1.md and return `(system_prompt, user_template)`.

    Uses the two markdown headers (`## SYSTEM PROMPT` and `## USER PROMPT
    TEMPLATE`) as anchors instead of splitting on `---` — the user template
    contains `---` horizontal rules between sections, so blind splitting
    would break.
    """
    text = _V1_MD_PATH.read_text(encoding="utf-8")

    sys_idx = text.find(_SYS_HEADER)
    user_idx = text.find(_USER_HEADER)

    if sys_idx == -1 or user_idx == -1:
        raise RuntimeError(
            f"{_V1_MD_PATH.name} must contain both {_SYS_HEADER!r} and {_USER_HEADER!r}"
        )
    if sys_idx >= user_idx:
        raise RuntimeError(f"{_V1_MD_PATH.name}: {_SYS_HEADER!r} must come before {_USER_HEADER!r}")

    system_section = text[sys_idx + len(_SYS_HEADER) : user_idx]
    user_section = text[user_idx + len(_USER_HEADER) :]

    system_prompt = _clean(system_section)
    user_template = _clean(user_section)

    if not system_prompt:
        raise RuntimeError(f"{_V1_MD_PATH.name}: system prompt section is empty")
    if not user_template:
        raise RuntimeError(f"{_V1_MD_PATH.name}: user template section is empty")
    if "{" not in user_template:
        raise RuntimeError(
            f"{_V1_MD_PATH.name}: user template has no placeholders — did you paste "
            "the wrong section?"
        )

    return system_prompt, user_template


def _clean(section: str) -> str:
    """Strip leading/trailing whitespace and any leading `---` separator lines.

    The body between headers typically starts with `\n\n---\n\n` and ends with
    `\n\n---\n\n`; we trim those so the prompt text is clean.
    """
    lines = section.strip().splitlines()
    # drop leading blank/separator lines
    while lines and lines[0].strip() in ("", "---"):
        lines.pop(0)
    # drop trailing blank/separator lines
    while lines and lines[-1].strip() in ("", "---"):
        lines.pop()
    return "\n".join(lines)


def _load_tool_schema() -> dict:
    with _TOOL_SCHEMA_PATH.open("r", encoding="utf-8") as f:
        schema = json.load(f)
    # Sanity check — the schema must have the Anthropic tool-use shape
    for key in ("name", "description", "input_schema"):
        if key not in schema:
            raise RuntimeError(f"{_TOOL_SCHEMA_PATH.name}: missing required key {key!r}")
    if schema["name"] != "submit_coaching":
        raise RuntimeError(
            f"{_TOOL_SCHEMA_PATH.name}: expected tool name 'submit_coaching', "
            f"got {schema['name']!r}"
        )
    return schema


# Load at import time so missing files fail loudly on first import, not on
# first coaching call.
SYSTEM_PROMPT, USER_TEMPLATE = _load_prompts()
COACHING_TOOL: dict = _load_tool_schema()

__all__ = ["COACHING_TOOL", "PROMPT_VERSION", "SYSTEM_PROMPT", "USER_TEMPLATE"]
