from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.agents.sheets_reader import (
    MAX_ROWS,
    _fetch_records,
    _parse_date,
    _parse_tags,
    _validate_headers,
    run,
)
from src.models import SheetsReaderInput

# ---------------------------------------------------------------------------
# _parse_date
# ---------------------------------------------------------------------------


def test_parse_date_iso() -> None:
    assert _parse_date("2024-03-15") == date(2024, 3, 15)


def test_parse_date_dmy() -> None:
    assert _parse_date("15/03/2024") == date(2024, 3, 15)


def test_parse_date_mdy() -> None:
    assert _parse_date("03/15/2024") == date(2024, 3, 15)


def test_parse_date_none() -> None:
    assert _parse_date(None) is None
    assert _parse_date("") is None


def test_parse_date_invalid() -> None:
    with pytest.raises(ValueError, match="Cannot parse date"):
        _parse_date("not-a-date")


# ---------------------------------------------------------------------------
# _parse_tags
# ---------------------------------------------------------------------------


def test_parse_tags_csv() -> None:
    assert _parse_tags("foo, bar, baz") == ["foo", "bar", "baz"]


def test_parse_tags_blank() -> None:
    assert _parse_tags(None) is None
    assert _parse_tags("") is None


def test_parse_tags_single() -> None:
    assert _parse_tags("only") == ["only"]


# ---------------------------------------------------------------------------
# _validate_headers
# ---------------------------------------------------------------------------


def test_validate_headers_ok() -> None:
    _validate_headers(
        ["contact_id", "name", "org", "client_context", "status", "last_contact_date",
         "relationship_strength", "email"]
    )


def test_validate_headers_missing_required() -> None:
    with pytest.raises(RuntimeError, match="Missing required columns"):
        _validate_headers(["contact_id", "name"])


def test_validate_headers_extra_columns_ok() -> None:
    # Unknown extra columns are tolerated (schema drift warning only)
    _validate_headers(
        ["contact_id", "name", "org", "client_context", "status", "last_contact_date",
         "relationship_strength", "extra_unknown_col"]
    )


# ---------------------------------------------------------------------------
# run() — full pipeline with a fake gspread client
# ---------------------------------------------------------------------------

VALID_ROW: dict[str, Any] = {
    "contact_id": "c001",
    "name": "Alice Example",
    "org": "Acme Corp",
    "client_context": "restoractive",
    "email": "alice@example.com",
    "phone": None,
    "linkedin_url": None,
    "status": "active",
    "last_contact_date": "2024-01-10",
    "next_action": "Send proposal",
    "next_action_due": "2024-02-01",
    "relationship_strength": "strong",
    "tags": "vip, prospect",
    "notes": None,
}


def _make_client(rows: list[dict[str, Any]]) -> MagicMock:
    """Build a minimal fake gspread client that returns given rows."""
    client = MagicMock()
    worksheet = MagicMock()
    worksheet.row_values.return_value = list(VALID_ROW.keys())
    worksheet.get_all_records.return_value = rows
    client.open_by_key.return_value.worksheet.return_value = worksheet
    return client


def _make_input(filter: str | None = None) -> SheetsReaderInput:
    return SheetsReaderInput(
        spreadsheet_id="sheet123",
        worksheet_name="Contacts",
        client_context_filter=filter,
    )


def test_run_happy_path() -> None:
    client = _make_client([VALID_ROW])
    result = run(_make_input(), client)

    assert len(result.contacts) == 1
    contact = result.contacts[0]
    assert contact.contact_id == "c001"
    assert contact.name == "Alice Example"
    assert contact.last_contact_date == date(2024, 1, 10)
    assert contact.next_action_due == date(2024, 2, 1)
    assert contact.tags == ["vip", "prospect"]
    assert result.data_quality_issues == []
    assert result.filtered_do_not_contact_ids == []
    assert result.contacts_without_email_ids == []
    assert "c001" in result.current_cell_snapshot


def test_run_filters_do_not_contact() -> None:
    dnc_row = {**VALID_ROW, "contact_id": "c002", "status": "do_not_contact"}
    client = _make_client([VALID_ROW, dnc_row])
    result = run(_make_input(), client)

    assert len(result.contacts) == 1
    assert result.filtered_do_not_contact_ids == ["c002"]
    assert "c002" not in result.current_cell_snapshot


def test_run_flags_missing_email() -> None:
    no_email = {**VALID_ROW, "contact_id": "c003", "email": None}
    client = _make_client([no_email])
    result = run(_make_input(), client)

    assert result.contacts_without_email_ids == ["c003"]
    assert result.contacts[0].email is None


def test_run_skips_blank_contact_id() -> None:
    bad_row = {**VALID_ROW, "contact_id": ""}
    client = _make_client([bad_row])
    result = run(_make_input(), client)

    assert result.contacts == []
    assert len(result.data_quality_issues) == 1
    assert result.data_quality_issues[0].field == "contact_id"


def test_run_detects_duplicate_contact_id() -> None:
    client = _make_client([VALID_ROW, VALID_ROW])
    result = run(_make_input(), client)

    assert len(result.contacts) == 1
    assert len(result.data_quality_issues) == 1
    assert "Duplicate" in result.data_quality_issues[0].issue


def test_run_bad_date_produces_issue() -> None:
    bad_date_row = {**VALID_ROW, "contact_id": "c004", "last_contact_date": "not-a-date"}
    client = _make_client([bad_date_row])
    result = run(_make_input(), client)

    assert result.contacts == []
    issues = [i for i in result.data_quality_issues if i.field == "last_contact_date"]
    assert len(issues) == 1
    assert issues[0].contact_id == "c004"


def test_run_invalid_enum_produces_issue() -> None:
    bad_enum_row = {**VALID_ROW, "contact_id": "c005", "status": "invalid_status"}
    client = _make_client([bad_enum_row])
    result = run(_make_input(), client)

    assert result.contacts == []
    assert any(i.contact_id == "c005" for i in result.data_quality_issues)


def test_run_client_context_filter() -> None:
    other_context_row = {**VALID_ROW, "contact_id": "c006", "client_context": "networking"}
    client = _make_client([VALID_ROW, other_context_row])
    result = run(_make_input(filter="restoractive"), client)

    assert len(result.contacts) == 1
    assert result.contacts[0].contact_id == "c001"


def test_run_enforces_row_limit() -> None:
    rows = [{**VALID_ROW, "contact_id": f"c{i:04d}"} for i in range(MAX_ROWS + 1)]
    client = _make_client(rows)
    with pytest.raises(RuntimeError, match="rows"):
        run(_make_input(), client)


def test_run_snapshot_captures_raw_values() -> None:
    client = _make_client([VALID_ROW])
    result = run(_make_input(), client)

    snapshot = result.current_cell_snapshot["c001"]
    assert snapshot["last_contact_date"] == "2024-01-10"  # raw string, not date object


def test_run_retry_on_5xx() -> None:
    """Verify _fetch_records retries on HTTP 5xx and succeeds on the third attempt."""
    from unittest.mock import patch

    from gspread.exceptions import APIError

    response_5xx = MagicMock()
    response_5xx.status_code = 503

    worksheet = MagicMock()
    side_effects: list[Any] = [
        APIError(response_5xx),
        APIError(response_5xx),
        [VALID_ROW],
    ]
    worksheet.get_all_records.side_effect = side_effects

    with patch("src.agents.sheets_reader.time.sleep"):
        records = _fetch_records(worksheet)

    assert records == [VALID_ROW]
    assert worksheet.get_all_records.call_count == 3


def test_run_aborts_after_3_consecutive_5xx() -> None:
    from unittest.mock import patch

    from gspread.exceptions import APIError

    response_5xx = MagicMock()
    response_5xx.status_code = 500

    worksheet = MagicMock()
    worksheet.get_all_records.side_effect = APIError(response_5xx)

    with patch("src.agents.sheets_reader.time.sleep"):
        with pytest.raises(RuntimeError, match="3 consecutive"):
            _fetch_records(worksheet)
