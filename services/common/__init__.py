"""Shared infrastructure used by every service package.

Purpose:    marks `services.common` as a package of cross-cutting building
            blocks: config, database, errors, events, contracts, security,
            storage, logging, Redis and time helpers.
Why needed: keeps infrastructure in one place so domain packages (session,
            speech, evaluation, ...) never duplicate it or depend on each other
            for it.
Contains:   no code, only this description.
Used by:    every service package; tests/conftest imports it directly.
Depends on: nothing.
"""
