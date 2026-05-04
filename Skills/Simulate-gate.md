Simulate a human review gate during development without blocking stdin.

The user will provide: $ARGUMENTS (gate name and decision: accept/reject/feedback)

Human gates in this project:
- Outreach draft approval before Gmail push: Every run producing outreach drafts. Drafts appear in Obsidian outreach-drafts.md with checkbox list. User explicitly invokes `--approve-drafts` CLI and answers per-draft interactive prompt.
- Gmail draft review before sending: After Gmail drafts created by --approve-drafts. User reviews each draft in Gmail Drafts folder before manually clicking Send.
- SAFE_ADMIN field run-level batch confirmation: `--apply-updates` CLI mode. safe_admin fields (last_contact_date, next_action, next_action_due) presented as a numbered batch summary showing all proposed changes before any write. User types 'confirm' or 'skip'.
- STATUS_FOLLOWUP field per-row approval: `--apply-updates` CLI mode. status and relationship_strength changes presented one row at a time showing: contact name, field, current live value, proposed value, rationale from review_analyst.
- Data quality remediation: sheets_reader data quality report identifies invalid enum values, missing required fields, or duplicate contact_ids. User reviews in weekly-review.md.
- Do-not-contact quarterly review: Every 90 days (tracked in data/.last-dnc-review timestamp file), audit summary lists all contacts with status=do_not_contact.

Steps:
1. Identify which gate $ARGUMENTS refers to
2. Inject the specified decision programmatically (patch stdin or use a --auto flag)
3. Run the pipeline through that gate and report what happens next
4. Never use gate simulation in production — development only