Preview the email/notification output without sending it.

The user will provide: $ARGUMENTS (optional: specific date or event to preview)

Steps:
1. Run the notification composer logic in dry-run mode (no actual send)
2. Print the full rendered email subject and body to the terminal
3. Flag any deadlines within 7 days that would trigger an alert
4. Do NOT send any emails — output only

If no dry-run flag exists yet, implement it before running.