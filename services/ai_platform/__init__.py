"""GD as part of the AI Interviewer platform (M14): its sign-in and its skill graph.

Purpose:    marks `services.ai_platform` as the package that connects GD to the
            surrounding AI Interviewer platform.
Why needed: GD runs standalone or inside the platform; everything that only
            matters in platform mode (single sign-on bridge, skill-graph
            reporting) lives here so the rest of GD stays platform-agnostic.
Contains:   no code, only this description (see bridge.py, deps.py,
            skill_graph.py).
Used by:    evaluation/service and tests/test_platform.
Depends on: nothing.
"""
