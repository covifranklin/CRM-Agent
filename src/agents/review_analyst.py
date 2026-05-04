from __future__ import annotations

import json
import time
from datetime import date
from typing import Any

import anthropic
from pydantic import ValidationError

from src.models import (
    Contact,
    CrmUpdateSuggestion,
    OutreachOpportunity,
    ReviewAnalystInput,
    ReviewAnalystOutput,
    StaleLead,
)

MODEL = "claude-sonnet-4-6"
MAX_RETRIES = 3
BACKOFF_BASE = 2.0
STALE_DAYS = 30
OUTREACH_DAYS = 14
MAX_TOKENS = 8192
OBSIDIAN_BODY_LIMIT = 500  # chars per note included in prompt

# Pricing for claude-sonnet-4-6 (USD per 1k tokens)
INPUT_PRICE_PER_1K = 0.003
OUTPUT_PRICE_PER_1K = 0.015

IDENTITY_FIELDS: frozenset[str] = frozenset({"contact_id", "name", "email"})
SAFE_ADMIN_FIELDS: frozenset[str] = frozenset(
    {"last_contact_date", "next_action", "next_action_due"}
)
STATUS_FOLLOWUP_FIELDS: frozenset[str] = frozenset({"status", "relationship_strength"})

# Relationship strength rank for sorting stale leads (higher = more strategic)
_STRENGTH_RANK: dict[str, int] = {"strong": 4, "medium": 3, "weak": 2, "new": 1}

_ANALYSIS_TOOL: dict[str, Any] = {
    "name": "submit_analysis",
    "description": "Submit the structured CRM analysis results.",
    "input_schema": {
        "type": "object",
        "properties": {
            "stale_leads": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "contact_id": {"type": "string"},
                        "name": {"type": "string"},
                        "last_contact_date": {
                            "type": "string",
                            "description": "ISO date YYYY-MM-DD",
                        },
                        "relationship_strength": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": [
                        "contact_id",
                        "name",
                        "last_contact_date",
                        "relationship_strength",
                        "reason",
                    ],
                },
            },
            "data_quality_report": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "contact_id": {"type": "string"},
                        "field": {"type": "string"},
                        "value": {"description": "The problematic value (any type)"},
                        "issue": {"type": "string"},
                    },
                    "required": ["field", "issue"],
                },
            },
            "outreach_opportunities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "contact_id": {"type": "string"},
                        "name": {"type": "string"},
                        "channel": {
                            "type": "string",
                            "description": "Recommended channel: 'email' or 'linkedin'",
                        },
                        "generate_email_draft": {"type": "boolean"},
                        "rationale": {"type": "string"},
                    },
                    "required": [
                        "contact_id",
                        "name",
                        "channel",
                        "generate_email_draft",
                        "rationale",
                    ],
                },
            },
            "crm_update_recommendations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "contact_id": {"type": "string"},
                        "field_name": {"type": "string"},
                        "new_value": {"description": "Proposed new value (any type)"},
                        "field_class": {
                            "type": "string",
                            "enum": ["SAFE_ADMIN", "STATUS_FOLLOWUP"],
                        },
                        "rationale": {"type": "string"},
                    },
                    "required": [
                        "contact_id",
                        "field_name",
                        "new_value",
                        "field_class",
                        "rationale",
                    ],
                },
            },
        },
        "required": [
            "stale_leads",
            "data_quality_report",
            "outreach_opportunities",
            "crm_update_recommendations",
        ],
    },
}


def _serialize_contact(contact: Contact, current_date: date) -> dict[str, Any]:
    days_since = (current_date - contact.last_contact_date).days
    days_until_due: int | None = None
    if contact.next_action_due:
        days_until_due = (contact.next_action_due - current_date).days

    data: dict[str, Any] = {
        "contact_id": contact.contact_id,
        "name": contact.name,
        "org": contact.org,
        "client_context": contact.client_context,
        "status": contact.status,
        "relationship_strength": contact.relationship_strength,
        "last_contact_date": contact.last_contact_date.isoformat(),
        "days_since_contact": days_since,
        "next_action": contact.next_action,
        "next_action_due": (
            contact.next_action_due.isoformat() if contact.next_action_due else None
        ),
        "days_until_due": days_until_due,
        "has_email": bool(contact.email),
        "has_linkedin": bool(contact.linkedin_url),
        "tags": contact.tags,
        "notes": contact.notes,
    }

    if contact.obsidian_context:
        data["obsidian_context"] = contact.obsidian_context.body[:OBSIDIAN_BODY_LIMIT]

    return data


def _build_prompt(inp: ReviewAnalystInput) -> str:
    contacts_data = [
        _serialize_contact(c, inp.current_date) for c in inp.enriched_contacts
    ]
    issues_data = [i.model_dump() for i in inp.data_quality_issues]
    gaps_data = [g.model_dump() for g in inp.coverage_gaps]

    stale_contact_ids = {
        c.contact_id
        for c in inp.enriched_contacts
        if (inp.current_date - c.last_contact_date).days > STALE_DAYS
    }
    due_soon_ids = {
        c.contact_id
        for c in inp.enriched_contacts
        if c.next_action_due
        and 0 <= (c.next_action_due - inp.current_date).days <= OUTREACH_DAYS
    }

    return f"""You are a CRM review analyst. Perform a weekly review of the contacts below.

Today: {inp.current_date.isoformat()}
Client context: {inp.client_context_filter or "all"}

FIELD RULES — strictly enforce:
- IDENTITY (never suggest changes): {sorted(IDENTITY_FIELDS)}
- SAFE_ADMIN (suggest freely): {sorted(SAFE_ADMIN_FIELDS)}
- STATUS_FOLLOWUP (suggest carefully, requires human approval): {sorted(STATUS_FOLLOWUP_FIELDS)}
- generate_email_draft: set true ONLY when channel="email" AND has_email=true

PRE-COMPUTED SIGNALS:
- Contacts with no contact in >{STALE_DAYS} days (stale): {sorted(stale_contact_ids)}
- Contacts with action due within {OUTREACH_DAYS} days: {sorted(due_soon_ids)}

---
## Contacts ({len(contacts_data)} total)

{json.dumps(contacts_data, indent=2, default=str)}

---
## Data Quality Issues ({len(issues_data)})

{json.dumps(issues_data, indent=2, default=str)}

---
## Coverage Gaps — contacts without Obsidian notes ({len(gaps_data)})

{json.dumps(gaps_data, indent=2, default=str)}

---

Return your analysis using the submit_analysis tool:
1. stale_leads — all contacts from the pre-computed stale list, ranked by strategic value \
(relationship_strength desc, then recency). Include a concise reason per lead.
2. data_quality_report — pass through all input issues; elevate critical ones in your reasons.
3. outreach_opportunities — contacts due soon OR weak/new relationships worth warming. \
Recommend channel. Set generate_email_draft per the rules above.
4. crm_update_recommendations — specific field updates with rationale. \
Only SAFE_ADMIN or STATUS_FOLLOWUP fields."""


def _extract_tool_input(response: anthropic.types.Message) -> dict[str, Any]:
    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_analysis":
            result: dict[str, Any] = block.input
            return result
    raise RuntimeError(
        "LLM did not call submit_analysis tool. "
        f"Response stop_reason: {response.stop_reason}"
    )


def _call_with_retry(
    client: anthropic.Anthropic,
    model: str,
    prompt: str,
) -> anthropic.types.Message:
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            return client.messages.create(  # type: ignore[call-overload, no-any-return]
                model=model,
                max_tokens=MAX_TOKENS,
                tools=[_ANALYSIS_TOOL],
                tool_choice={"type": "tool", "name": "submit_analysis"},
                messages=[{"role": "user", "content": prompt}],
            )
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
            is_retryable = isinstance(exc, anthropic.APIConnectionError) or (
                isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500
            )
            if not is_retryable:
                raise
            last_error = exc
            if attempt < MAX_RETRIES - 1:
                time.sleep(BACKOFF_BASE**attempt)

    raise RuntimeError(
        f"Anthropic API failed on {MAX_RETRIES} consecutive attempts. "
        f"Last error: {last_error}"
    )


def _post_process(
    raw: ReviewAnalystOutput,
    known_ids: frozenset[str],
    contacts_with_email: frozenset[str],
) -> ReviewAnalystOutput:
    """Enforce hard constraints on LLM output before returning."""

    # Filter IDENTITY field updates (should never appear; log if found)
    safe_updates: list[CrmUpdateSuggestion] = []
    for suggestion in raw.crm_update_recommendations:
        if suggestion.field_name in IDENTITY_FIELDS:
            # SECURITY_VIOLATION — discard silently; orchestrator logs this
            continue
        safe_updates.append(suggestion)

    # Fix generate_email_draft for contacts without email
    safe_outreach: list[OutreachOpportunity] = []
    for opp in raw.outreach_opportunities:
        if opp.generate_email_draft and opp.contact_id not in contacts_with_email:
            opp = opp.model_copy(update={"generate_email_draft": False})
        safe_outreach.append(opp)

    # Drop recommendations for unknown contact_ids (hallucinated contacts)
    safe_stale: list[StaleLead] = [
        s for s in raw.stale_leads if s.contact_id in known_ids
    ]
    safe_outreach = [o for o in safe_outreach if o.contact_id in known_ids]
    safe_updates = [u for u in safe_updates if u.contact_id in known_ids]

    return ReviewAnalystOutput(
        stale_leads=safe_stale,
        data_quality_report=raw.data_quality_report,
        outreach_opportunities=safe_outreach,
        crm_update_recommendations=safe_updates,
    )


def _compute_cost(usage: anthropic.types.Usage) -> float:
    return (usage.input_tokens / 1000 * INPUT_PRICE_PER_1K) + (
        usage.output_tokens / 1000 * OUTPUT_PRICE_PER_1K
    )


def run(
    inp: ReviewAnalystInput,
    client: anthropic.Anthropic,
    model: str = MODEL,
    max_cost_usd: float = 1.00,
) -> ReviewAnalystOutput:
    known_ids = frozenset(c.contact_id for c in inp.enriched_contacts)
    contacts_with_email = frozenset(
        c.contact_id for c in inp.enriched_contacts if c.email
    )

    prompt = _build_prompt(inp)
    response = _call_with_retry(client, model, prompt)

    cost = _compute_cost(response.usage)
    if cost > max_cost_usd:
        raise RuntimeError(
            f"LLM call cost ${cost:.4f} exceeds limit ${max_cost_usd:.2f}. "
            "Aborting to protect budget."
        )

    raw_input = _extract_tool_input(response)

    try:
        raw_output = ReviewAnalystOutput.model_validate(raw_input)
    except ValidationError as exc:
        raise RuntimeError(
            f"LLM returned output that failed schema validation: {exc}"
        ) from exc

    return _post_process(raw_output, known_ids, contacts_with_email)
