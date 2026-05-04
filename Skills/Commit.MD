Create a focused git commit for the current changes.

Before committing:
1. Run `pytest` — all tests must pass
2. Run the linter and fix any issues
3. Stage only files relevant to the current change — never `git add -A` blindly
4. Commit message: explain *why*, not *what* (the diff shows what)
5. Never commit `.env`, API keys, or secrets

If tests fail, fix the root cause. Do not use `--no-verify`.
