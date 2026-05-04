from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel


class ObsidianContext(BaseModel):
    frontmatter: dict[str, Any]
    body: str
    file_path: str


class Contact(BaseModel):
    contact_id: str
    name: str
    org: str
    client_context: Literal["restoractive", "glass_squid", "networking", "personal"]
    email: str | None = None
    phone: str | None = None
    linkedin_url: str | None = None
    status: Literal["active", "warm", "cold", "dormant", "do_not_contact"]
    last_contact_date: date
    next_action: str | None = None
    next_action_due: date | None = None
    relationship_strength: Literal["strong", "medium", "weak", "new"]
    tags: list[str] | None = None
    notes: str | None = None
    obsidian_context: ObsidianContext | None = None


class DataQualityIssue(BaseModel):
    contact_id: str | None = None
    field: str
    value: Any
    issue: str


class CoverageGap(BaseModel):
    contact_id: str
    reason: str


class OrphanNote(BaseModel):
    file_path: str
    contact_id_inferred: str | None = None


class StaleLead(BaseModel):
    contact_id: str
    name: str
    last_contact_date: date
    relationship_strength: str
    reason: str


class CrmUpdateSuggestion(BaseModel):
    contact_id: str
    field_name: str
    new_value: Any
    field_class: Literal["SAFE_ADMIN", "STATUS_FOLLOWUP"]
    rationale: str


class OutreachOpportunity(BaseModel):
    contact_id: str
    name: str
    channel: str
    generate_email_draft: bool
    rationale: str


class DraftEmail(BaseModel):
    contact_id: str
    name: str
    to_email: str
    subject_line: str
    draft_body_html: str
    draft_body_plain: str
    rationale: str
    context_sources_used: list[str]
    crm_updates: list[CrmUpdateSuggestion]


class PendingUpdateEntry(BaseModel):
    contact_id: str
    field_name: str
    old_value: Any
    new_value: Any
    field_class: Literal["SAFE_ADMIN", "STATUS_FOLLOWUP"]
    status: Literal["pending", "applied", "skipped", "deferred", "conflict"]
    error: str | None = None


class DraftStatusEntry(BaseModel):
    contact_id: str
    status: Literal[
        "proposed", "approved-pending", "gmail-created", "sent", "replied", "rejected"
    ]
    gmail_draft_id: str | None = None
    timestamp: datetime
    rationale: str | None = None


class AuditLogEntry(BaseModel):
    event_type: str
    timestamp: datetime
    details: dict[str, Any]


# --- Agent I/O models ---


class SheetsReaderInput(BaseModel):
    spreadsheet_id: str
    worksheet_name: str
    client_context_filter: str | None = None


class SheetsReaderOutput(BaseModel):
    contacts: list[Contact]
    data_quality_issues: list[DataQualityIssue]
    filtered_do_not_contact_ids: list[str]
    contacts_without_email_ids: list[str]
    current_cell_snapshot: dict[str, dict[str, Any]]


class ContextLinkerInput(BaseModel):
    contacts: list[Contact]
    vault_path: str


class ContextLinkerOutput(BaseModel):
    enriched_contacts: list[Contact]
    coverage_gaps: list[CoverageGap]
    orphan_notes: list[OrphanNote]


class ReviewAnalystInput(BaseModel):
    enriched_contacts: list[Contact]
    data_quality_issues: list[DataQualityIssue]
    coverage_gaps: list[CoverageGap]
    current_date: date
    client_context_filter: str | None = None


class ReviewAnalystOutput(BaseModel):
    stale_leads: list[StaleLead]
    data_quality_report: list[DataQualityIssue]
    outreach_opportunities: list[OutreachOpportunity]
    crm_update_recommendations: list[CrmUpdateSuggestion]


class DraftWriterInput(BaseModel):
    outreach_opportunities: list[OutreachOpportunity]
    obsidian_context_notes: dict[str, ObsidianContext]
    user_name: str
    user_role: str


class DraftWriterOutput(BaseModel):
    draft_emails: list[DraftEmail]


class ReportFormatterInput(BaseModel):
    review_analyst_output: ReviewAnalystOutput
    draft_writer_output: DraftWriterOutput
    run_metadata: dict[str, Any]
    audit_log_entries: list[AuditLogEntry]


class ReportFormatterOutput(BaseModel):
    status: str


class GmailDrafterInput(BaseModel):
    approved_drafts_status: list[DraftStatusEntry]
    draft_contents: list[DraftEmail]


class GmailDrafterOutput(BaseModel):
    updated_draft_statuses: list[DraftStatusEntry]


class SheetsUpdaterInput(BaseModel):
    pending_updates: list[PendingUpdateEntry]
    current_cell_snapshot: dict[str, dict[str, Any]]
    user_approvals: dict[str, Any]


class SheetsUpdaterOutput(BaseModel):
    sheets_write_results: dict[str, Any]
    audit_log_entries: list[AuditLogEntry]
    updated_pending_updates: list[PendingUpdateEntry]


class SnapshotExporterInput(BaseModel):
    spreadsheet_id: str
    output_path: str


class SnapshotExporterOutput(BaseModel):
    status: str
    file_path: str
