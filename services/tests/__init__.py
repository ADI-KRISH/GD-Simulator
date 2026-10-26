"""Test suite package (run with `pytest`; no Docker, Redis, model or API key needed).

Purpose:    marks `services/tests` as a package so test modules can import each
            other's helpers and pytest resolves them consistently.
Why needed: without it, same-named test files in different folders could clash.
Contains:   no code, only this description. Shared fixtures live in conftest.py.
Used by:    pytest.
Depends on: nothing.
"""
