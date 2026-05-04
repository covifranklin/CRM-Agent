from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.agents.review_analyst import (
    STALE_DAYS,
    _build_prompt,
    _compute_cost,
    _post_process,
    _serialize_contact,
    run,
)
from src.models import (
    Contact,
    CrmUpdateSuggestion,
    DataQualityIssue,
    OutreachOpportunity,
    ReviewAnalystInput,
    ReviewAnalystOutput,
    StaleLead,
)

TODAY = date(2024, 6, 1)
STALE_DATE = TODAY - timedelta(days=STALE_DAYS + 5)
RECENT_DATE = TODAY - timedelta(days=5)


def _make_contact(
    contact_id: str = "c001",
    name: str = "Alice",
    status: str = "active",
    relationship_strength: str = "strong",
    last_contact_date: date = RECENT_DATE,
    email: str | None = "alice@example.com",
    next_action_due: date | None = None,
    linkedin_url: str | None = None,
) -> Contact:
    return Contact(
        contact_id=contact_id,
        name=name,
        org="Acme",
        client_context="restoractive",
        email=email,
        linkedin_url=linkedin_url,
        status=status,  # type: ignore[arg-type]
        last_contact_date=last_contact_date,
        next_action_due=next_action_due,
        relationship_strength=relationship_strength,  # type: ignore[arg-type]
    )


def _make_input(
    contacts: list[Contact] | None = None,
    issues: list[DataQualityIssue] | None = None,
) -> ReviewAnalystInput:
    return ReviewAnalystInput(
        enriched_contacts=contacts or [_make_contact()],
        data_quality_issues=issues or [],
        coverage_gaps=[],
        current_date=TODAY,
        client_context_filter="restoractive",
    )


def _make_valid_llm_output(
    stale_leads: list[dict[str, Any]] | None = None,
    outreach: list[dict[str, Any]] | None = None,
    updates: list[dict[str, Any]] | None = None,
    dq: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "stale_leads": stale_leads or [],
        "data_quality_report": dq or [],
        "outreach_opportunities": outreach or [],
        "crm_update_recommendations": updates or [],
    }


def _make_mock_client(tool_input: dict[str, Any], cost: tuple[int, int] = (1000, 500)) -> MagicMock:
    """Build a fake anthropic.Anthropic client that returns tool_input as tool call."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = "submit_analysis"
    block.input = tool_input

    usage = MagicMock()
    usage.input_tokens = cost[0]
    usage.output_tokens = cost[1]

    response = MagicMock()
    response.content = [block]
    response.usage = usage
    response.stop_reason = "tool_use"

    client = MagicMock()
    client.messages.create.return_value = response
    return client


# ---------------------------------------------------------------------------
# _serialize_contact
# ---------------------------------------------------------------------------


def test_serialize_contact_days_since() -> None:
    c = _make_contact(last_contact_date=TODAY - timedelta(days=10))
    data = _serialize_contact(c, TODAY)
    assert data["days_since_contact"] == 10


def test_serialize_contact_days_until_due() -> None:
    c = _make_contact(next_action_due=TODAY + timedelta(days=7))
    data = _serialize_contact(c, TODAY)
    assert data["days_until_due"] == 7


def test_serialize_contact_overdue() -> None:
    c = _make_contact(next_action_due=TODAY - timedelta(days=3))
    data = _serialize_contact(c, TODAY)
    assert data["days_until_due"] == -3


def test_serialize_contact_has_email_flag() -> None:
    with_email = _make_contact(email="x@x.com")
    without_email = _make_contact(email=None)
    assert _serialize_contact(with_email, TODAY)["has_email"] is True
    assert _serialize_contact(without_email, TODAY)["has_email"] is False


def test_serialize_contact_no_email_field_exposed() -> None:
    c = _make_contact(email="secret@example.com")
    data = _serialize_contact(c, TODAY)
    assert "email" not in data


def test_serialize_contact_obsidian_truncated() -> None:
    from src.models import ObsidianContext

    long_body = "x" * 1000
    c = _make_contact()
    c = c.model_copy(
        update={"obsidian_context": ObsidianContext(frontmatter={}, body=long_body, file_path="/f")}
    )
    data = _serialize_contact(c, TODAY)
    assert len(data["obsidian_context"]) == 500


# ---------------------------------------------------------------------------
# _build_prompt
# ---------------------------------------------------------------------------


def test_build_prompt_includes_stale_ids() -> None:
    stale = _make_contact(contact_id="c_stale", last_contact_date=STALE_DATE)
    fresh = _make_contact(contact_id="c_fresh", last_contact_date=RECENT_DATE)
    inp = _make_input(contacts=[stale, fresh])
    prompt = _build_prompt(inp)
    assert "c_stale" in prompt
    assert "c_fresh" not in prompt.split("PRE-COMPUTED")[1].split("##")[0]


def test_build_prompt_includes_today() -> None:
    prompt = _build_prompt(_make_input())
    assert TODAY.isoformat() in prompt


# ---------------------------------------------------------------------------
# _compute_cost
# ---------------------------------------------------------------------------


def test_compute_cost() -> None:
    usage = MagicMock()
    usage.input_tokens = 1000
    usage.output_tokens = 1000
    cost = _compute_cost(usage)
    assert abs(cost - 0.018) < 1e-6  # 1k * 0.003 + 1k * 0.015


# ---------------------------------------------------------------------------
# _post_process
# ---------------------------------------------------------------------------


def test_post_process_removes_identity_field_updates() -> None:
    raw = ReviewAnalystOutput(
        stale_leads=[],
        data_quality_report=[],
        outreach_opportunities=[],
        crm_update_recommendations=[
            CrmUpdateSuggestion(
                contact_id="c001",
                field_name="email",  # IDENTITY — must be removed
                new_value="new@example.com",
                field_class="SAFE_ADMIN",
                rationale="test",
            ),
            CrmUpdateSuggestion(
                contact_id="c001",
                field_name="next_action",  # SAFE_ADMIN — must survive
                new_value="follow up",
                field_class="SAFE_ADMIN",
                rationale="test",
            ),
        ],
    )
    result = _post_process(raw, frozenset({"c001"}), frozenset({"c001"}))
    fields = [u.field_name for u in result.crm_update_recommendations]
    assert "email" not in fields
    assert "next_action" in fields


def test_post_process_fixes_email_draft_without_email() -> None:
    raw = ReviewAnalystOutput(
        stale_leads=[],
        data_quality_report=[],
        outreach_opportunities=[
            OutreachOpportunity(
                contact_id="c_noemail",
                name="No Email",
                channel="email",
                generate_email_draft=True,  # should be corrected
                rationale="test",
            )
        ],
        crm_update_recommendations=[],
    )
    result = _post_process(raw, frozenset({"c_noemail"}), frozenset())
    assert result.outreach_opportunities[0].generate_email_draft is False


def test_post_process_drops_hallucinated_contact_ids() -> None:
    raw = ReviewAnalystOutput(
        stale_leads=[
            StaleLead(
                contact_id="ghost",
                name="Ghost",
                last_contact_date=STALE_DATE,
                relationship_strength="weak",
                reason="invented",
            )
        ],
        data_quality_report=[],
        outreach_opportunities=[],
        crm_update_recommendations=[],
    )
    result = _post_process(raw, frozenset({"c001"}), frozenset())
    assert result.stale_leads == []


# ---------------------------------------------------------------------------
# run() — full pipeline with mock Anthropic client
# ---------------------------------------------------------------------------


def test_run_happy_path() -> None:
    tool_input = _make_valid_llm_output(
        stale_leads=[
            {
                "contact_id": "c001",
                "name": "Alice",
                "last_contact_date": STALE_DATE.isoformat(),
                "relationship_strength": "strong",
                "reason": "No contact in 35 days",
            }
        ]
    )
    contact = _make_contact(last_contact_date=STALE_DATE)
    client = _make_mock_client(tool_input)

    result = run(_make_input(contacts=[contact]), client)

    assert isinstance(result, ReviewAnalystOutput)
    assert len(result.stale_leads) == 1
    assert result.stale_leads[0].contact_id == "c001"


def test_run_returns_empty_lists_when_no_issues() -> None:
    client = _make_mock_client(_make_valid_llm_output())
    result = run(_make_input(), client)
    assert result.stale_leads == []
    assert result.outreach_opportunities == []


def test_run_retry_on_5xx() -> None:
    import anthropic as sdk

    response = MagicMock()
    response.status_code = 503
    block = MagicMock()
    block.type = "tool_use"
    block.name = "submit_analysis"
    block.input = _make_valid_llm_output()
    success_resp = MagicMock()
    success_resp.content = [block]
    success_resp.usage = MagicMock(input_tokens=100, output_tokens=50)
    success_resp.stop_reason = "tool_use"

    client = MagicMock()
    client.messages.create.side_effect = [
        sdk.APIStatusError("503", response=response, body={}),
        sdk.APIStatusError("503", response=response, body={}),
        success_resp,
    ]

    with patch("src.agents.review_analyst.time.sleep"):
        result = run(_make_input(), client)

    assert isinstance(result, ReviewAnalystOutput)
    assert client.messages.create.call_count == 3


def test_run_aborts_after_3_consecutive_5xx() -> None:
    import anthropic as sdk

    response = MagicMock()
    response.status_code = 500
    client = MagicMock()
    client.messages.create.side_effect = sdk.APIStatusError(
        "500", response=response, body={}
    )

    with patch("src.agents.review_analyst.time.sleep"):
        with pytest.raises(RuntimeError, match="3 consecutive"):
            run(_make_input(), client)


def test_run_does_not_retry_4xx() -> None:
    import anthropic as sdk

    response = MagicMock()
    response.status_code = 400
    client = MagicMock()
    client.messages.create.side_effect = sdk.APIStatusError(
        "400", response=response, body={}
    )

    with pytest.raises(sdk.APIStatusError):
        run(_make_input(), client)

    assert client.messages.create.call_count == 1


def test_run_raises_when_no_tool_call() -> None:
    text_block = MagicMock()
    text_block.type = "text"

    response = MagicMock()
    response.content = [text_block]
    response.usage = MagicMock(input_tokens=100, output_tokens=10)
    response.stop_reason = "end_turn"

    client = MagicMock()
    client.messages.create.return_value = response

    with pytest.raises(RuntimeError, match="did not call submit_analysis"):
        run(_make_input(), client)


def test_run_raises_on_validation_error() -> None:
    bad_input: dict[str, Any] = {
        "stale_leads": "not_a_list",  # wrong type
        "data_quality_report": [],
        "outreach_opportunities": [],
        "crm_update_recommendations": [],
    }
    client = _make_mock_client(bad_input)
    with pytest.raises(RuntimeError, match="schema validation"):
        run(_make_input(), client)


def test_run_enforces_cost_limit() -> None:
    # Simulate a very expensive call: 100k input + 50k output tokens
    client = _make_mock_client(
        _make_valid_llm_output(), cost=(100_000, 50_000)
    )
    with pytest.raises(RuntimeError, match="exceeds limit"):
        run(_make_input(), client, max_cost_usd=0.10)


def test_run_post_processing_applied() -> None:
    tool_input = _make_valid_llm_output(
        updates=[
            {
                "contact_id": "c001",
                "field_name": "name",  # IDENTITY — must be stripped
                "new_value": "Alicia",
                "field_class": "SAFE_ADMIN",
                "rationale": "test",
            }
        ]
    )
    client = _make_mock_client(tool_input)
    result = run(_make_input(), client)
    assert result.crm_update_recommendations == []
