from __future__ import annotations

import time
from typing import Any

import anthropic
from pydantic import ValidationError

from src.models import (
    CrmUpdateSuggestion,
    DraftEmail,
    DraftWriterInput,
    DraftWriterOutput,
    ObsidianContext,
    OutreachOpportunity,
)

MODEL = "claude-sonnet-4-6"
MAX_RETRIES = 3
BACKOFF_BASE = 2.0
MAX_TOKENS = 4096
OBSIDIAN_BODY_LIMIT = 1500  # chars — more context than review_analyst needs

# Pricing for claude-sonnet-4-6 (USD per 1k tokens)
INPUT_PRICE_PER_1K = 0.003
OUTPUT_PRICE_PER_1K = 0.015

# draft_writer only ever suggests SAFE_ADMIN updates
SAFE_ADMIN_FIELDS: frozenset[str] = frozenset(
    {"last_contact_date", "next_action", "next_action_due"}
)

_DRAFT_TOOL: dict[str, Any] = {
    "name": "submit_draft",
    "description": "Submit the generated outreach email draft.",
    "input_schema": {
        "type": "object",
        "properties": {
            "subject_line": {"type": "string"},
            "draft_body_html": {
                "type": "string",
                "description": "Email body as HTML — body content only, no <html>/<head> wrapper.",
            },
            "draft_body_plain": {
                "type": "string",
                "description": "Same content as plain text.",
            },
            "rationale": {
                "type": "string",
                "description": (
                    "Brief note on personalisation sources used. "
                    "Flag 'thin-context' if Obsidian notes were absent or sparse."
                ),
            },
            "context_sources_used": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of Obsidian file paths or note sections drawn from.",
            },
            "crm_updates": {
                "type": "array",
                "description": "SAFE_ADMIN field updates only.",
                "items": {
                    "type": "object",
                    "properties": {
                        "field_name": {
                            "type": "string",
                            "enum": ["last_contact_date", "next_action", "next_action_due"],
                        },
                        "new_value": {"description": "Proposed new value (string or ISO date)."},
                        "rationale": {"type": "string"},
                    },
                    "required": ["field_name", "new_value", "rationale"],
                },
            },
        },
        "required": [
            "subject_line",
            "draft_body_html",
            "draft_body_plain",
            "rationale",
            "context_sources_used",
            "crm_updates",
        ],
    },
}


def _extract_email(context: ObsidianContext | None) -> str:
    if context is None:
        return ""
    return str(context.frontmatter.get("email", "")).strip()


def _build_prompt(
    opp: OutreachOpportunity,
    context: ObsidianContext | None,
    user_name: str,
    user_role: str,
) -> str:
    context_block: str
    if context:
        body_excerpt = context.body[:OBSIDIAN_BODY_LIMIT]
        fm_lines = "\n".join(
            f"  {k}: {v}"
            for k, v in context.frontmatter.items()
            if k not in ("email", "phone")  # exclude private fields
        )
        context_block = (
            f"Obsidian note: {context.file_path}\n"
            f"Frontmatter:\n{fm_lines}\n\n"
            f"Note body:\n{body_excerpt}"
        )
    else:
        context_block = (
            "No Obsidian note available for this contact. "
            "Flag this as thin-context in your rationale."
        )

    return f"""You are writing a professional outreach email on behalf of {user_name} ({user_role}).

Contact: {opp.name}
Outreach rationale: {opp.rationale}

## Obsidian context
{context_block}

---

Write a warm, concise outreach email. Rules:
- Subject line: specific and non-salesy (no "checking in" or "following up" clichés)
- Body: max 3 short paragraphs, conversational tone, written in first person as {user_name}
- Personalise using specific details from the Obsidian notes where available
- draft_body_html: body content only — no <html>/<head> tags, use <p> and <br> tags
- draft_body_plain: identical content as plain text
- crm_updates: always suggest last_contact_date (today's ISO date), next_action, next_action_due
- context_sources_used: list the file path and any specific note sections referenced
- rationale: one sentence on what context drove the personalisation; \
say "thin-context" if notes were absent

Use the submit_draft tool to return your output."""


def _extract_tool_input(response: anthropic.types.Message) -> dict[str, Any]:
    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_draft":
            result: dict[str, Any] = block.input
            return result
    raise RuntimeError(
        "LLM did not call submit_draft tool. "
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
                tools=[_DRAFT_TOOL],
                tool_choice={"type": "tool", "name": "submit_draft"},
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


def _compute_cost(usage: anthropic.types.Usage) -> float:
    return (usage.input_tokens / 1000 * INPUT_PRICE_PER_1K) + (
        usage.output_tokens / 1000 * OUTPUT_PRICE_PER_1K
    )


def _assemble_draft(
    raw: dict[str, Any],
    opp: OutreachOpportunity,
    to_email: str,
) -> DraftEmail:
    """Construct a DraftEmail from LLM tool output, enforcing SAFE_ADMIN-only updates."""
    crm_updates: list[CrmUpdateSuggestion] = []
    for update in raw.get("crm_updates", []):
        field = str(update.get("field_name", "")).strip()
        if field not in SAFE_ADMIN_FIELDS:
            continue
        crm_updates.append(
            CrmUpdateSuggestion(
                contact_id=opp.contact_id,
                field_name=field,
                new_value=update.get("new_value"),
                field_class="SAFE_ADMIN",
                rationale=str(update.get("rationale", "")),
            )
        )

    try:
        return DraftEmail(
            contact_id=opp.contact_id,
            name=opp.name,
            to_email=to_email,
            subject_line=raw["subject_line"],
            draft_body_html=raw["draft_body_html"],
            draft_body_plain=raw["draft_body_plain"],
            rationale=raw["rationale"],
            context_sources_used=raw.get("context_sources_used", []),
            crm_updates=crm_updates,
        )
    except (KeyError, ValidationError) as exc:
        raise RuntimeError(
            f"LLM draft for {opp.contact_id} failed assembly: {exc}"
        ) from exc


def run(
    inp: DraftWriterInput,
    client: anthropic.Anthropic,
    model: str = MODEL,
    max_cost_usd: float = 1.00,
) -> DraftWriterOutput:
    drafts: list[DraftEmail] = []
    cumulative_cost = 0.0

    email_ops = [opp for opp in inp.outreach_opportunities if opp.generate_email_draft]

    for opp in email_ops:
        context = inp.obsidian_context_notes.get(opp.contact_id)
        to_email = _extract_email(context)

        prompt = _build_prompt(opp, context, inp.user_name, inp.user_role)
        response = _call_with_retry(client, model, prompt)

        cost = _compute_cost(response.usage)
        cumulative_cost += cost

        if cumulative_cost > max_cost_usd:
            raise RuntimeError(
                f"Cumulative LLM cost ${cumulative_cost:.4f} exceeds limit "
                f"${max_cost_usd:.2f}. "
                f"Completed {len(drafts)}/{len(email_ops)} drafts before aborting."
            )

        raw_input = _extract_tool_input(response)
        draft = _assemble_draft(raw_input, opp, to_email)
        drafts.append(draft)

    return DraftWriterOutput(draft_emails=drafts)
