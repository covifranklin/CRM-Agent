from __future__ import annotations

from datetime import date
from pathlib import Path

from src.agents.context_linker import _parse_frontmatter, run
from src.models import Contact, ContextLinkerInput

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

BASE_CONTACT = Contact(
    contact_id="c001",
    name="Alice Example",
    org="Acme Corp",
    client_context="restoractive",
    email="alice@example.com",
    status="active",
    last_contact_date=date(2024, 1, 10),
    relationship_strength="strong",
)

SECOND_CONTACT = Contact(
    contact_id="c002",
    name="Bob Builder",
    org="Builders Ltd",
    client_context="networking",
    status="warm",
    last_contact_date=date(2024, 2, 1),
    relationship_strength="medium",
)


def _make_inp(contacts: list[Contact], vault_path: str) -> ContextLinkerInput:
    return ContextLinkerInput(contacts=contacts, vault_path=vault_path)


def _write_note(contacts_dir: Path, filename: str, content: str) -> Path:
    contacts_dir.mkdir(parents=True, exist_ok=True)
    p = contacts_dir / filename
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# _parse_frontmatter
# ---------------------------------------------------------------------------


def test_parse_frontmatter_valid() -> None:
    content = "---\ncontact_id: c001\ntags: [vip]\n---\n\nSome body text."
    fm, body = _parse_frontmatter(content)
    assert fm == {"contact_id": "c001", "tags": ["vip"]}
    assert body == "Some body text."


def test_parse_frontmatter_no_delimiters() -> None:
    content = "Just body, no frontmatter."
    fm, body = _parse_frontmatter(content)
    assert fm == {}
    assert body == content


def test_parse_frontmatter_unclosed() -> None:
    content = "---\ncontact_id: c001\n"
    fm, body = _parse_frontmatter(content)
    assert fm == {}


def test_parse_frontmatter_invalid_yaml() -> None:
    content = "---\n: bad: yaml: [\n---\n\nBody."
    fm, body = _parse_frontmatter(content)
    assert fm == {}
    assert body == "Body."


def test_parse_frontmatter_non_dict_yaml() -> None:
    content = "---\n- item1\n- item2\n---\n\nBody."
    fm, body = _parse_frontmatter(content)
    assert fm == {}


def test_parse_frontmatter_empty_frontmatter() -> None:
    content = "---\n---\n\nBody only."
    fm, body = _parse_frontmatter(content)
    assert fm == {}
    assert body == "Body only."


# ---------------------------------------------------------------------------
# run() — vault scanning and enrichment
# ---------------------------------------------------------------------------


def test_run_enriches_via_frontmatter(tmp_path: Path) -> None:
    contacts_dir = tmp_path / "CRM" / "Contacts"
    _write_note(
        contacts_dir,
        "alice.md",
        "---\ncontact_id: c001\n---\n\nMet at conference.",
    )

    result = run(_make_inp([BASE_CONTACT], str(tmp_path)))

    assert len(result.enriched_contacts) == 1
    ctx = result.enriched_contacts[0].obsidian_context
    assert ctx is not None
    assert ctx.frontmatter["contact_id"] == "c001"
    assert ctx.body == "Met at conference."
    assert ctx.file_path.endswith("alice.md")
    assert result.coverage_gaps == []
    assert result.orphan_notes == []


def test_run_enriches_via_filename(tmp_path: Path) -> None:
    contacts_dir = tmp_path / "CRM" / "Contacts"
    _write_note(contacts_dir, "c001.md", "No frontmatter here.\n\nJust notes.")

    result = run(_make_inp([BASE_CONTACT], str(tmp_path)))

    ctx = result.enriched_contacts[0].obsidian_context
    assert ctx is not None
    assert "Just notes." in ctx.body
    assert result.coverage_gaps == []


def test_run_coverage_gap_when_no_note(tmp_path: Path) -> None:
    (tmp_path / "CRM" / "Contacts").mkdir(parents=True)

    result = run(_make_inp([BASE_CONTACT], str(tmp_path)))

    assert result.enriched_contacts[0].obsidian_context is None
    assert len(result.coverage_gaps) == 1
    assert result.coverage_gaps[0].contact_id == "c001"


def test_run_coverage_gap_when_vault_missing(tmp_path: Path) -> None:
    result = run(_make_inp([BASE_CONTACT], str(tmp_path / "nonexistent")))

    assert len(result.coverage_gaps) == 1
    assert "not found" in result.coverage_gaps[0].reason


def test_run_orphan_note_no_matching_contact(tmp_path: Path) -> None:
    contacts_dir = tmp_path / "CRM" / "Contacts"
    _write_note(contacts_dir, "ghost.md", "---\ncontact_id: c999\n---\n\nOrphan.")

    result = run(_make_inp([BASE_CONTACT], str(tmp_path)))

    assert len(result.orphan_notes) == 1
    assert result.orphan_notes[0].contact_id_inferred == "c999"
    assert len(result.coverage_gaps) == 1  # c001 has no note


def test_run_orphan_note_no_frontmatter_no_match(tmp_path: Path) -> None:
    contacts_dir = tmp_path / "CRM" / "Contacts"
    _write_note(contacts_dir, "random_note.md", "Just some text with no id.")

    result = run(_make_inp([BASE_CONTACT], str(tmp_path)))

    assert len(result.orphan_notes) == 1
    assert result.orphan_notes[0].contact_id_inferred is None


def test_run_duplicate_note_second_is_orphan(tmp_path: Path) -> None:
    contacts_dir = tmp_path / "CRM" / "Contacts"
    _write_note(contacts_dir, "alice.md", "---\ncontact_id: c001\n---\n\nFirst note.")
    _write_note(contacts_dir, "alice2.md", "---\ncontact_id: c001\n---\n\nSecond note.")

    result = run(_make_inp([BASE_CONTACT], str(tmp_path)))

    assert result.enriched_contacts[0].obsidian_context is not None
    assert len(result.orphan_notes) == 1
    assert result.orphan_notes[0].contact_id_inferred == "c001"


def test_run_multiple_contacts_mixed(tmp_path: Path) -> None:
    contacts_dir = tmp_path / "CRM" / "Contacts"
    _write_note(
        contacts_dir,
        "alice.md",
        "---\ncontact_id: c001\n---\n\nAlice context.",
    )
    # c002 has no note

    result = run(_make_inp([BASE_CONTACT, SECOND_CONTACT], str(tmp_path)))

    enriched = {c.contact_id: c for c in result.enriched_contacts}
    assert enriched["c001"].obsidian_context is not None
    assert enriched["c002"].obsidian_context is None
    assert len(result.coverage_gaps) == 1
    assert result.coverage_gaps[0].contact_id == "c002"
    assert result.orphan_notes == []


def test_run_unreadable_file_becomes_orphan(tmp_path: Path) -> None:
    contacts_dir = tmp_path / "CRM" / "Contacts"
    contacts_dir.mkdir(parents=True)
    bad_file = contacts_dir / "unreadable.md"
    bad_file.write_text("content")
    bad_file.chmod(0o000)

    try:
        result = run(_make_inp([BASE_CONTACT], str(tmp_path)))
        orphan_paths = [o.file_path for o in result.orphan_notes]
        assert str(bad_file) in orphan_paths
    finally:
        bad_file.chmod(0o644)


def test_run_empty_vault_dir(tmp_path: Path) -> None:
    (tmp_path / "CRM" / "Contacts").mkdir(parents=True)

    result = run(_make_inp([BASE_CONTACT, SECOND_CONTACT], str(tmp_path)))

    assert len(result.coverage_gaps) == 2
    assert result.orphan_notes == []
    assert all(c.obsidian_context is None for c in result.enriched_contacts)


def test_run_preserves_contact_order(tmp_path: Path) -> None:
    contacts_dir = tmp_path / "CRM" / "Contacts"
    _write_note(contacts_dir, "c002.md", "Bob note.")

    contacts = [BASE_CONTACT, SECOND_CONTACT]
    result = run(_make_inp(contacts, str(tmp_path)))

    assert [c.contact_id for c in result.enriched_contacts] == ["c001", "c002"]


def test_run_no_contacts_no_crash(tmp_path: Path) -> None:
    result = run(_make_inp([], str(tmp_path)))

    assert result.enriched_contacts == []
    assert result.coverage_gaps == []
