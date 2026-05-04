Add a new record to the project data store (the yaml data store).

The user will provide: $ARGUMENTS

Steps:
1. Read the existing data store to understand the current schema and format
2. Parse $ARGUMENTS into the correct fields for a record record
3. Validate the new entry against the relevant Pydantic model
4. Write the entry to the data store, preserving existing entries
5. Run `pytest tests/test_contracts.py -v` to confirm validation passes
6. Confirm the record was added and show the final record
