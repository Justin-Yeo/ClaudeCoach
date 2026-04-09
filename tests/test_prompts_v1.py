"""Tests for `app.prompts.v1`."""

from __future__ import annotations

from app.prompts.v1 import (
    COACHING_TOOL,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    USER_TEMPLATE,
)


class TestPromptLoading:
    def test_system_prompt_nonempty(self):
        assert len(SYSTEM_PROMPT) > 500  # substantial text

    def test_system_prompt_contains_key_rules(self):
        # A few sentinel strings from the prompt body — if these go missing
        # the loader has broken something.
        assert "submit_coaching" in SYSTEM_PROMPT
        assert "available_days" in SYSTEM_PROMPT
        assert "taxonomy" in SYSTEM_PROMPT.lower()

    def test_user_template_has_placeholders(self):
        # Spot-check a few of the required placeholders
        for placeholder in (
            "{today_local}",
            "{age}",
            "{weight_kg}",
            "{max_hr}",
            "{goals_block}",
            "{recent_runs_block}",
            "{distance_km}",
            "{avg_pace_label}",
        ):
            assert placeholder in USER_TEMPLATE, f"missing {placeholder!r} in USER_TEMPLATE"

    def test_user_template_contains_runner_profile_heading(self):
        assert "### Runner Profile" in USER_TEMPLATE

    def test_sections_are_disjoint(self):
        # The loader should not have leaked the header line into the body
        assert "## SYSTEM PROMPT" not in SYSTEM_PROMPT
        assert "## USER PROMPT TEMPLATE" not in USER_TEMPLATE
        # The user template header shouldn't bleed into the system prompt either
        assert "## USER PROMPT TEMPLATE" not in SYSTEM_PROMPT


class TestToolSchema:
    def test_tool_name(self):
        assert COACHING_TOOL["name"] == "submit_coaching"

    def test_tool_has_description(self):
        assert len(COACHING_TOOL["description"]) > 10

    def test_input_schema_structure(self):
        schema = COACHING_TOOL["input_schema"]
        assert schema["type"] == "object"
        required = schema["required"]
        for field in (
            "run_type",
            "load_rating",
            "flags",
            "post_run_review",
            "next_session",
        ):
            assert field in required

    def test_run_type_enum(self):
        run_type = COACHING_TOOL["input_schema"]["properties"]["run_type"]
        assert run_type["type"] == "string"
        assert set(run_type["enum"]) == {
            "easy",
            "long",
            "tempo",
            "intervals",
            "recovery",
            "race",
        }

    def test_post_run_review_shape(self):
        review = COACHING_TOOL["input_schema"]["properties"]["post_run_review"]
        required = review["required"]
        for field in ("run_summary", "went_well", "to_watch", "digest"):
            assert field in required

    def test_next_session_shape(self):
        ns = COACHING_TOOL["input_schema"]["properties"]["next_session"]
        required = ns["required"]
        for field in (
            "type",
            "scheduled_date",
            "scheduled_day_label",
            "relative_offset_days",
            "distance_km",
            "target_pace_min_km",
            "workout",
        ):
            assert field in required

    def test_workout_shape(self):
        workout = COACHING_TOOL["input_schema"]["properties"]["next_session"]["properties"][
            "workout"
        ]
        for field in ("warmup", "main", "cooldown"):
            assert field in workout["required"]


def test_prompt_version_is_v1():
    assert PROMPT_VERSION == "v1"
