"""GD platform backend services.

Locally these all run inside a single FastAPI process (see `services.main`).
Each subpackage is kept independently deployable so the split in design §7 stays
available later — but there is no deploy step in this project, only localhost.
"""
