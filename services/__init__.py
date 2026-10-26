"""GD platform backend services.

Locally these all run inside a single FastAPI process (see `services.main`).
Each subpackage is kept independently deployable so the split in design §7 stays
available later — but there is no deploy step in this project, only localhost.

Purpose:    marks `services` as the top-level Python package of the backend.
Why needed: every import in the project is written as `services.<package>...`;
            without this file those absolute imports would not resolve.
Contains:   no code, only this description.
Used by:    every module (via the `services.` import prefix); the graph lists
            tests/test_simulation as the one explicit importer.
Depends on: nothing.
"""
