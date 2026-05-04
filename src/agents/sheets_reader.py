from __future__ import annotations

import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import gspread
from gspread.exceptions import APIError
from pydantic import ValidationError

from src.models import (
    Contact,
    DataQualityIssue,
    SheetsReaderInput,
    SheetsReaderOutput,
)

DEFAULT_TOKEN_PATH = Path.home() / ".crm-review" / "google_token.json"
MAX_ROWS = 1_000
MAX_RETRIES = 3
BACKOFF_BASE = 2.0  # seconds

# Columns expected in the sheet header row (lowercase, matching Contact field names)
EXPECTED_COLUMNS: frozenset[str] = frozenset({
    "contact_id", "name", "org", "client_context",
    "email", "phone", "linkedin_url", "status",
    "last_contact_date", "next_action", "next_action_due",
    "relationship_strength", "tags", "notes",
})

REQUIRED_COLUMNS: frozenset[str] = frozenset({
    "contact_id", "name", "org", "client_context",
    "status", "last_contact_date", "relationship_strength",
})

# Fields that must never be overwritten by sheets_updater (used by snapshot)
IDENTITY_FIELDS: frozenset[str] = frozenset({"contact_id", "name", "email"})


def build_client(token_path: Path = DEFAULT_TOKEN_PATH) -> gspread.Client:
    """Load stored OAuth token and return an authenticated gspread client."""
    from google.auth.transport.requests import Request as AuthRequest
    from google.oauth2.credentials import Credentials

    if not token_path.exists():
        raise FileNotFoundError(
            f"OAuth token not found at {token_path}. Run `python setup_google.py` first."
        )
    creds: Credentials = Credentials.from_authorized_user_file(  # type: ignore[no-untyped-call]
        str(token_path)
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(AuthRequest())  # type: ignore[no-untyped-call]
        token_path.write_text(creds.to_json())  # type: ignore[no-untyped-call]
    return gspread.authorize(creds)


def _validate_headers(actual: list[str]) -> None:
    """Raise RuntimeError with a column diff if required columns are absent."""
    actual_set = frozenset(col.lower().strip() for col in actual)
    missing = REQUIRED_COLUMNS - actual_set
    if not missing:
        return
    unknown = actual_set - EXPECTED_COLUMNS
    parts = [f"Missing required columns: {sorted(missing)}"]
    if unknown:
        parts.append(f"Unexpected columns present: {sorted(unknown)}")
    raise RuntimeError("Sheet schema validation failed:\n" + "\n".join(parts))


def _fetch_records(worksheet: gspread.Worksheet) -> list[dict[str, Any]]:
    """Fetch all rows with exponential backoff. Aborts after 3 consecutive 5xx errors."""
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            records: list[dict[str, Any]] = worksheet.get_all_records(
                default_blank=None
            )
            return records
        except APIError as exc:
            response = exc.response
            status: int = response.status_code if response is not None else 0
            if 500 <= status < 600:
                last_error = exc
                if attempt < MAX_RETRIES - 1:
                    time.sleep(BACKOFF_BASE**attempt)
            else:
                raise
    raise RuntimeError(
        f"Sheets API failed on {MAX_RETRIES} consecutive attempts (HTTP 5xx). "
        f"Last error: {last_error}"
    )


def _parse_date(raw: object) -> date | None:
    """Return a date parsed from a cell value, None if blank, raises ValueError if invalid."""
    if raw is None or raw == "":
        return None
    s = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date from {s!r} — expected YYYY-MM-DD or DD/MM/YYYY")


def _parse_tags(raw: object) -> list[str] | None:
    """Parse comma-separated tags string into a list, or None if blank."""
    if raw is None or raw == "":
        return None
    return [t.strip() for t in str(raw).split(",") if t.strip()]


def run(inp: SheetsReaderInput, client: gspread.Client) -> SheetsReaderOutput:
    spreadsheet = client.open_by_key(inp.spreadsheet_id)
    worksheet = spreadsheet.worksheet(inp.worksheet_name)

    _validate_headers(worksheet.row_values(1))

    records = _fetch_records(worksheet)

    if len(records) > MAX_ROWS:
        raise RuntimeError(
            f"Sheet returned {len(records)} rows (limit {MAX_ROWS}). "
            "Likely a config error or wrong spreadsheet — aborting."
        )

    contacts: list[Contact] = []
    data_quality_issues: list[DataQualityIssue] = []
    filtered_do_not_contact_ids: list[str] = []
    contacts_without_email_ids: list[str] = []
    current_cell_snapshot: dict[str, dict[str, Any]] = {}
    seen_contact_ids: dict[str, int] = {}

    for row_idx, row in enumerate(records, start=2):  # row 1 is the header
        contact_id = str(row.get("contact_id") or "").strip()

        if not contact_id:
            data_quality_issues.append(
                DataQualityIssue(
                    contact_id=None,
                    field="contact_id",
                    value=row.get("contact_id"),
                    issue="Blank contact_id — row skipped",
                )
            )
            continue

        if contact_id in seen_contact_ids:
            data_quality_issues.append(
                DataQualityIssue(
                    contact_id=contact_id,
                    field="contact_id",
                    value=contact_id,
                    issue=(
                        f"Duplicate contact_id "
                        f"(first seen at row {seen_contact_ids[contact_id]})"
                    ),
                )
            )
            continue
        seen_contact_ids[contact_id] = row_idx

        # Do-not-contact rows are silently removed from all downstream output
        if str(row.get("status") or "").strip() == "do_not_contact":
            filtered_do_not_contact_ids.append(contact_id)
            continue

        # Optional client_context filter — skip rows that don't match
        if inp.client_context_filter:
            row_context = str(row.get("client_context") or "").strip().lower()
            if row_context != inp.client_context_filter.strip().lower():
                continue

        # Mutate a copy so raw cell snapshot stays pristine
        raw: dict[str, Any] = dict(row)

        try:
            parsed_last = _parse_date(raw.get("last_contact_date"))
            if parsed_last is None:
                raise ValueError("last_contact_date is required and was blank")
            raw["last_contact_date"] = parsed_last
        except ValueError as exc:
            data_quality_issues.append(
                DataQualityIssue(
                    contact_id=contact_id,
                    field="last_contact_date",
                    value=row.get("last_contact_date"),
                    issue=str(exc),
                )
            )
            continue

        try:
            raw["next_action_due"] = _parse_date(raw.get("next_action_due"))
        except ValueError as exc:
            data_quality_issues.append(
                DataQualityIssue(
                    contact_id=contact_id,
                    field="next_action_due",
                    value=row.get("next_action_due"),
                    issue=str(exc),
                )
            )
            continue

        raw["tags"] = _parse_tags(raw.get("tags"))

        try:
            contact = Contact.model_validate(raw)
        except ValidationError as exc:
            for error in exc.errors():
                field = ".".join(str(loc) for loc in error["loc"])
                data_quality_issues.append(
                    DataQualityIssue(
                        contact_id=contact_id,
                        field=field,
                        value=raw.get(field),
                        issue=error["msg"],
                    )
                )
            continue

        # Snapshot raw cell values at read time for later conflict detection
        current_cell_snapshot[contact_id] = dict(row)

        contacts.append(contact)
        if not contact.email:
            contacts_without_email_ids.append(contact_id)

    return SheetsReaderOutput(
        contacts=contacts,
        data_quality_issues=data_quality_issues,
        filtered_do_not_contact_ids=filtered_do_not_contact_ids,
        contacts_without_email_ids=contacts_without_email_ids,
        current_cell_snapshot=current_cell_snapshot,
    )
