# Design and build a practical CRM + AI Agent workflow that lets me:
1. Maintain a

## What This Is
A lightweight CRM workflow combining Google Sheets as structured data source, Obsidian as relationship context layer, and an AI agent that performs weekly review cycles — scanning for stale leads, data quality issues, and outreach opportunities — then producing draft messages and recommendation reports for human approval before any sending occurs. Designed for Restoractive first, reusable for Glass Squid and strategic networking later.
*MVP | Viability assessed at 82%*

## Commands
- **Run:** `Local single-user MVP: (1) Python 3.11+, pip, Obsidian vault, Google account. (2) pip install -r requirements.txt (pandas, jinja2, anthropic, pyyaml, google-auth-oauthlib, google-api-python-client, gspread). (3) Google OAuth setup: run `python setup_google.py` — configures OAuth for gmail.compose + spreadsheets scopes in one browser flow. Token at ~/.crm-review/google_token.json with 600 permissions. (4) config.yaml: vault_path, spreadsheet_id, worksheet_name, user_name, user_role, client_context_filter, anthropic_api_key. No csv_path in normal config; csv_path optional for --export-snapshot output directory. (5) setup_vault.py creates Obsidian folder structure. (6) Full analysis (reads live Sheet): `python crm_review.py --config config.yaml`. (7) Review in Obsidian: read weekly-review.md and outreach-drafts.md. (8) Approve drafts: `python crm_review.py --approve-drafts --config config.yaml`. (9) Apply CRM updates: `python crm_review.py --apply-updates --config config.yaml` (safe_admin run-level confirm, status_followup per-row). (10) Export snapshot (backup/debug only): `python crm_review.py --export-snapshot --config config.yaml`. (11) Record outcomes: `python crm_review.py --record-outcome --contact jane-doe --outcome sent --config config.yaml`. (12) Weekly cron: `0 9 * * 1 python crm_review.py --config config.yaml`. (13) Nudge cron: `0 10 * * 3 python nudge_check.py`.`
- **Test:** `pytest`
- **Audit log:** Append-only structured JSON at data/runs/{run_id}/audit-log.json, one JSON object per line per event, ISO timestamps on all entries. Human-readable summary written to vault/CRM Reviews/{date}-audit-summary.md. draft-status.json at data/runs/{run_id}/draft-status.json tracks full draft lifecycle from proposed to gmail-created.

## Hard Constraints
- Processes professional contact data (names, emails, phones, LinkedIn URLs). (1) GDPR/Privacy: contact data leaves the local machine only via Anthropic API calls (names + context passed in prompts) and Google API calls (Sheets read/write, Gmail draft creation). User should review Anthropic API data retention policy (currently zero data retention available via API agreement) and Google API terms. (2) Do-not-contact: quadruple safety layer — filtered at sheets_reader ingest, excluded from LLM prompts, output scan post-LLM, and gmail_drafter re-check before draft.create. sheets_updater also skips do-not-contact rows. (3) Google scope minimization: gmail.compose (draft creation only — no send, read, or delete) + spreadsheets (read/write configured sheet only). OAuth consent screen should list both scopes explicitly. (4) No automated sending: all Gmail drafts require manual Send action by the user in Gmail UI. (5) Sheets write safety: IDENTITY field list enforced in code; conflict detection prevents overwriting concurrent edits; dry-run mode available before first live write-back. (6) Health or sensitive data in Obsidian notes: if vault/CRM/Contacts/ notes contain health information, it will be included in Anthropic API calls. User should avoid storing health data in contact notes or explicitly accept this risk. (7) CAN-SPAM / outreach compliance: system supports personal professional outreach, not bulk marketing. User is responsible for ensuring outreach complies with applicable regulations.
- Terminate run if cumulative LLM API cost (tracked after each API call using token counts * per-token price from config) exceeds $1.00; write partial results to audit-log.json before exit
- Terminate if sheets_reader returns more than 1000 rows — likely config error or wrong spreadsheet
- Terminate if any do-not-contact contact_id appears in review_analyst or draft_writer output JSON — log as SECURITY_VIOLATION and exit
- Terminate if 3 consecutive Anthropic API errors (HTTP 5xx or timeout) — no infinite retry; log final error and exit
- gmail_drafter: halt all draft creation on OAuth auth error — print token refresh instructions and exit with code 2
- gmail_drafter: abort entire draft batch if any do-not-contact contact_id found in approved draft list
- sheets_updater: abort entire pending-updates.json batch if any IDENTITY field appears — log as SECURITY_VIOLATION
- sheets_updater: abort if 3 consecutive Sheets API write failures (HTTP 5xx) — log partial success, preserve pending-updates.json for retry

## Human Review Gates — Never Skip
- Before Outreach draft approval before Gmail push: Every run producing outreach drafts. Drafts appear in Obsidian outreach-drafts.md with checkbox list. User explicitly invokes `--approve-drafts` CLI and answers per-draft interactive prompt.
- Before Gmail draft review before sending: After Gmail drafts created by --approve-drafts. User reviews each draft in Gmail Drafts folder before manually clicking Send.
- Before SAFE_ADMIN field run-level batch confirmation: `--apply-updates` CLI mode. safe_admin fields (last_contact_date, next_action, next_action_due) presented as a numbered batch summary showing all proposed changes before any write. User types 'confirm' or 'skip'.
- Before STATUS_FOLLOWUP field per-row approval: `--apply-updates` CLI mode. status and relationship_strength changes presented one row at a time showing: contact name, field, current live value, proposed value, rationale from review_analyst.
- Before Data quality remediation: sheets_reader data quality report identifies invalid enum values, missing required fields, or duplicate contact_ids. User reviews in weekly-review.md.
- Before Do-not-contact quarterly review: Every 90 days (tracked in data/.last-dnc-review timestamp file), audit summary lists all contacts with status=do_not_contact.

## Known Failure Modes (watch for these)
- Sheets schema drift: user modifies Sheet columns or renames headers. Mitigation: sheets_reader validates header row against config-defined schema on every run; clear error with column diff shown before pipeline proceeds.
- Sheets API read quota exceeded: 500 req/100s per user; typical run uses ~5 read requests. Mitigation: single batch read of all rows at run start; retry with exponential backoff up to 3 attempts; abort with clear message if quota hit.
- Sheets API write conflict: user edits Sheet between pipeline read and write-back. Mitigation: sheets_updater compares current cell value to snapshot captured at run start; if value changed, skips write and logs conflict with CONFLICT flag in CLI output.
- Obsidian context thin: drafts generic. Mitigation: draft_writer flags thin-context in rationale; report highlights coverage gaps.

## Never Do This
- Write any cell in any Sheet
- Read spreadsheets other than configured spreadsheet_id
- Access Obsidian vault
- Make any non-Sheets network call
- Access Gmail API
- Write to vault