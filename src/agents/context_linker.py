from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from src.models import (
    Contact,
    ContextLinkerInput,
    ContextLinkerOutput,
    CoverageGap,
    ObsidianContext,
    OrphanNote,
)

# Relative path inside the vault where contact notes live
CONTACTS_SUBDIR = Path("CRM") / "Contacts"


def _parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Split a markdown file into YAML frontmatter dict and body string.

    Returns ({}, full_content) when no frontmatter delimiters are found.
    YAML parse errors are swallowed — the file is still usable as body-only context.
    """
    if not content.startswith("---"):
        return {}, content

    end = content.find("\n---", 3)
    if end == -1:
        return {}, content

    yaml_str = content[3:end].strip()
    body = content[end + 4 :].lstrip("\n")

    try:
        parsed = yaml.safe_load(yaml_str)
        fm: dict[str, Any] = parsed if isinstance(parsed, dict) else {}
    except yaml.YAMLError:
        fm = {}

    return fm, body


def _read_note(path: Path) -> tuple[dict[str, Any], str] | None:
    """Read and parse a single markdown note. Returns None on any read error."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return _parse_frontmatter(content)


def _infer_contact_id(
    fm: dict[str, Any],
    filename_stem: str,
    known_ids: frozenset[str],
) -> str | None:
    """Return a contact_id inferred from frontmatter or filename, or None."""
    fm_id = str(fm.get("contact_id", "")).strip()
    if fm_id:
        return fm_id
    if filename_stem in known_ids:
        return filename_stem
    return None


def run(inp: ContextLinkerInput) -> ContextLinkerOutput:
    contacts_dir = Path(inp.vault_path) / CONTACTS_SUBDIR
    known_ids: frozenset[str] = frozenset(c.contact_id for c in inp.contacts)

    # Scan vault and build contact_id -> (frontmatter, body, path) map
    note_map: dict[str, tuple[dict[str, Any], str, Path]] = {}
    orphan_notes: list[OrphanNote] = []

    if contacts_dir.exists():
        for md_path in sorted(contacts_dir.glob("*.md")):
            result = _read_note(md_path)
            if result is None:
                orphan_notes.append(
                    OrphanNote(file_path=str(md_path), contact_id_inferred=None)
                )
                continue

            fm, body = result
            contact_id = _infer_contact_id(fm, md_path.stem, known_ids)

            if contact_id and contact_id in known_ids:
                if contact_id not in note_map:
                    note_map[contact_id] = (fm, body, md_path)
                else:
                    # Second note for the same contact_id is an orphan
                    orphan_notes.append(
                        OrphanNote(
                            file_path=str(md_path),
                            contact_id_inferred=contact_id,
                        )
                    )
            else:
                orphan_notes.append(
                    OrphanNote(
                        file_path=str(md_path),
                        contact_id_inferred=contact_id,
                    )
                )

    # Enrich contacts and collect coverage gaps
    enriched_contacts: list[Contact] = []
    coverage_gaps: list[CoverageGap] = []

    missing_reason = (
        f"Vault contacts directory not found: {contacts_dir}"
        if not contacts_dir.exists()
        else "No matching note in vault/CRM/Contacts/"
    )

    for contact in inp.contacts:
        if contact.contact_id in note_map:
            fm, body, path = note_map[contact.contact_id]
            enriched = contact.model_copy(
                update={
                    "obsidian_context": ObsidianContext(
                        frontmatter=fm,
                        body=body,
                        file_path=str(path),
                    )
                }
            )
            enriched_contacts.append(enriched)
        else:
            coverage_gaps.append(
                CoverageGap(contact_id=contact.contact_id, reason=missing_reason)
            )
            enriched_contacts.append(contact)

    return ContextLinkerOutput(
        enriched_contacts=enriched_contacts,
        coverage_gaps=coverage_gaps,
        orphan_notes=orphan_notes,
    )
