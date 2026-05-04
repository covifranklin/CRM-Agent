from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.agents.draft_writer import (
    _assemble_draft,
    _build_prompt,
    _compute_cost,
    _extract_email,
    run,
)
from src.models import (
    DraftWriterInput,
    ObsidianContext,
    OutreachOpportunity,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_opp(
    contact_id: str = "c001",
    name: str = "Alice",
    channel: str = "email",
    generate_email_draft: bool = True,
    rationale: str = "Due for follow-up",
) -> OutreachOpportunity:
    return OutreachOpportunity(
        contact_id=contact_id,
        name=name,
        channel=channel,
        generate_email_draft=generate_email_draft,
        rationale=rationale,
    )


def _make_context(
    body: str = "Met at conference. Building restoration project.",
    file_path: str = "/vault/CRM/Contacts/alice.md",
    frontmatter: dict[str, Any] | None = None,
) -> ObsidianContext:
    fm: dict[str, Any] = frontmatter if frontmatter is not None else {"email": "alice@example.com"}
    return ObsidianContext(frontmatter=fm, body=body, file_path=file_path)


def _make_input(
    opportunities: list[OutreachOpportunity] | None = None,
    context_notes: dict[str, ObsidianContext] | None = None,
) -> DraftWriterInput:
    opps = opportunities if opportunities is not None else [_make_opp()]
    notes = context_notes if context_notes is not None else {"c001": _make_context()}
    return DraftWriterInput(
        outreach_opportunities=opps,
        obsidian_context_notes=notes,
        user_name="Covi",
        user_role="Founder",
    )


VALID_TOOL_OUTPUT: dict[str, Any] = {
    "subject_line": "Quick catch-up on the restoration project",
    "draft_body_html": "<p>Hi Alice,</p><p>Hope you're well...</p>",
    "draft_body_plain": "Hi Alice,\n\nHope you're well...",
    "rationale": "Used conference meeting note from Obsidian.",
    "context_sources_used": ["/vault/CRM/Contacts/alice.md"],
    "crm_updates": [
        {
            "field_name": "last_contact_date",
            "new_value": "2024-06-01",
            "rationale": "Sending this email counts as contact.",
        },
        {
            "field_name": "next_action",
            "new_value": "Follow up on response",
            "rationale": "Standard follow-up after outreach.",
        },
        {
            "field_name": "next_action_due",
            "new_value": "2024-06-15",
            "rationale": "Two weeks from today.",
        },
    ],
}


def _make_mock_client(
    tool_output: dict[str, Any] = VALID_TOOL_OUTPUT,
    cost: tuple[int, int] = (500, 300),
) -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.name = "submit_draft"
    block.input = tool_output

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
# _extract_email
# ---------------------------------------------------------------------------


def test_extract_email_from_frontmatter() -> None:
    ctx = _make_context(frontmatter={"email": "alice@example.com"})
    assert _extract_email(ctx) == "alice@example.com"


def test_extract_email_missing_returns_empty() -> None:
    ctx = _make_context(frontmatter={"contact_id": "c001"})
    assert _extract_email(ctx) == ""


def test_extract_email_no_context_returns_empty() -> None:
    assert _extract_email(None) == ""


# ---------------------------------------------------------------------------
# _build_prompt
# ---------------------------------------------------------------------------


def test_build_prompt_includes_contact_name() -> None:
    opp = _make_opp(name="Bob")
    prompt = _build_prompt(opp, _make_context(), "Covi", "Founder")
    assert "Bob" in prompt


def test_build_prompt_includes_obsidian_body() -> None:
    ctx = _make_context(body="Restoration project in Bristol.")
    prompt = _build_prompt(_make_opp(), ctx, "Covi", "Founder")
    assert "Bristol" in prompt


def test_build_prompt_truncates_long_body() -> None:
    ctx = _make_context(body="x" * 2000)
    prompt = _build_prompt(_make_opp(), ctx, "Covi", "Founder")
    assert "x" * 2000 not in prompt
    assert "x" * 1500 in prompt


def test_build_prompt_flags_thin_context_when_no_notes() -> None:
    prompt = _build_prompt(_make_opp(), None, "Covi", "Founder")
    assert "thin-context" in prompt.lower() or "no obsidian note" in prompt.lower()


def test_build_prompt_excludes_email_from_frontmatter() -> None:
    ctx = _make_context(frontmatter={"email": "secret@test.com", "org": "Acme"})
    prompt = _build_prompt(_make_opp(), ctx, "Covi", "Founder")
    assert "secret@test.com" not in prompt


# ---------------------------------------------------------------------------
# _compute_cost
# ---------------------------------------------------------------------------


def test_compute_cost_calculation() -> None:
    usage = MagicMock()
    usage.input_tokens = 500
    usage.output_tokens = 300
    cost = _compute_cost(usage)
    assert abs(cost - (0.5 * 0.003 + 0.3 * 0.015)) < 1e-9


# ---------------------------------------------------------------------------
# _assemble_draft
# ---------------------------------------------------------------------------


def test_assemble_draft_sets_contact_id_and_name() -> None:
    opp = _make_opp(contact_id="c001", name="Alice")
    draft = _assemble_draft(VALID_TOOL_OUTPUT, opp, "alice@example.com")
    assert draft.contact_id == "c001"
    assert draft.name == "Alice"
    assert draft.to_email == "alice@example.com"


def test_assemble_draft_strips_non_safe_admin_updates() -> None:
    raw = {
        **VALID_TOOL_OUTPUT,
        "crm_updates": [
            {"field_name": "status", "new_value": "warm", "rationale": "bad"},
            {"field_name": "next_action", "new_value": "follow up", "rationale": "ok"},
        ],
    }
    draft = _assemble_draft(raw, _make_opp(), "alice@example.com")
    fields = [u.field_name for u in draft.crm_updates]
    assert "status" not in fields
    assert "next_action" in fields


def test_assemble_draft_crm_updates_all_safe_admin() -> None:
    draft = _assemble_draft(VALID_TOOL_OUTPUT, _make_opp(), "alice@example.com")
    for update in draft.crm_updates:
        assert update.field_class == "SAFE_ADMIN"
        assert update.contact_id == "c001"


def test_assemble_draft_empty_crm_updates_ok() -> None:
    raw = {**VALID_TOOL_OUTPUT, "crm_updates": []}
    draft = _assemble_draft(raw, _make_opp(), "alice@example.com")
    assert draft.crm_updates == []


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------


def test_run_happy_path() -> None:
    client = _make_mock_client()
    result = run(_make_input(), client)

    assert len(result.draft_emails) == 1
    draft = result.draft_emails[0]
    assert draft.contact_id == "c001"
    assert draft.to_email == "alice@example.com"
    assert draft.subject_line == VALID_TOOL_OUTPUT["subject_line"]
    assert len(draft.crm_updates) == 3


def test_run_skips_non_email_opportunities() -> None:
    linkedin_opp = _make_opp(channel="linkedin", generate_email_draft=False)
    client = _make_mock_client()
    result = run(_make_input(opportunities=[linkedin_opp]), client)

    assert result.draft_emails == []
    client.messages.create.assert_not_called()


def test_run_processes_multiple_drafts() -> None:
    opps = [
        _make_opp(contact_id="c001", name="Alice"),
        _make_opp(contact_id="c002", name="Bob"),
    ]
    ctx2 = _make_context(frontmatter={"email": "bob@example.com"}, file_path="/vault/bob.md")
    client = _make_mock_client()
    notes = {"c001": _make_context(), "c002": ctx2}
    result = run(_make_input(opportunities=opps, context_notes=notes), client)

    assert len(result.draft_emails) == 2
    assert client.messages.create.call_count == 2


def test_run_uses_empty_email_when_no_frontmatter() -> None:
    ctx = _make_context(frontmatter={})  # no email key
    client = _make_mock_client()
    result = run(_make_input(context_notes={"c001": ctx}), client)

    assert result.draft_emails[0].to_email == ""


def test_run_uses_empty_email_when_no_context() -> None:
    client = _make_mock_client()
    result = run(_make_input(context_notes={}), client)

    assert result.draft_emails[0].to_email == ""


def test_run_retry_on_5xx() -> None:
    import anthropic as sdk

    resp_err = MagicMock()
    resp_err.status_code = 503

    block = MagicMock()
    block.type = "tool_use"
    block.name = "submit_draft"
    block.input = VALID_TOOL_OUTPUT
    success = MagicMock()
    success.content = [block]
    success.usage = MagicMock(input_tokens=100, output_tokens=50)
    success.stop_reason = "tool_use"

    client = MagicMock()
    client.messages.create.side_effect = [
        sdk.APIStatusError("503", response=resp_err, body={}),
        success,
    ]

    with patch("src.agents.draft_writer.time.sleep"):
        result = run(_make_input(), client)

    assert len(result.draft_emails) == 1
    assert client.messages.create.call_count == 2


def test_run_aborts_after_3_consecutive_5xx() -> None:
    import anthropic as sdk

    resp_err = MagicMock()
    resp_err.status_code = 500
    client = MagicMock()
    client.messages.create.side_effect = sdk.APIStatusError(
        "500", response=resp_err, body={}
    )

    with patch("src.agents.draft_writer.time.sleep"):
        with pytest.raises(RuntimeError, match="3 consecutive"):
            run(_make_input(), client)


def test_run_does_not_retry_4xx() -> None:
    import anthropic as sdk

    resp_err = MagicMock()
    resp_err.status_code = 400
    client = MagicMock()
    client.messages.create.side_effect = sdk.APIStatusError(
        "400", response=resp_err, body={}
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

    with pytest.raises(RuntimeError, match="did not call submit_draft"):
        run(_make_input(), client)


def test_run_enforces_cumulative_cost_limit() -> None:
    # Each call costs ~$0.09 → two calls exceed $0.10 limit
    client = _make_mock_client(cost=(3_000, 5_000))
    with pytest.raises(RuntimeError, match="exceeds limit"):
        run(_make_input(), client, max_cost_usd=0.01)


def test_run_cost_accumulates_across_drafts() -> None:
    # Two cheap drafts each costing ~$0.006 — both complete under $0.10 limit
    opps = [
        _make_opp(contact_id="c001", name="Alice"),
        _make_opp(contact_id="c002", name="Bob"),
    ]
    ctx2 = _make_context(frontmatter={"email": "bob@example.com"}, file_path="/vault/bob.md")
    client = _make_mock_client(cost=(200, 200))
    result = run(
        _make_input(opportunities=opps, context_notes={"c001": _make_context(), "c002": ctx2}),
        client,
        max_cost_usd=0.10,
    )
    assert len(result.draft_emails) == 2


def test_run_empty_opportunities_returns_empty() -> None:
    client = _make_mock_client()
    result = run(_make_input(opportunities=[]), client)

    assert result.draft_emails == []
    client.messages.create.assert_not_called()
