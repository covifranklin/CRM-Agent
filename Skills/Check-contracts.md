Validate all Pydantic models in the project.

Steps:
1. Run `pytest tests/test_contracts.py -v`
2. For each failure: read `src/contracts/models.py`, find the broken field
3. Fix the model — never relax a constraint to make a test pass; find the root cause
4. Re-run until all green
5. Report which models were checked and any fixes made

Run this after every Pydantic model change before touching anything else.