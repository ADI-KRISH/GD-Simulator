"""
Purpose: Marks services/realtime as a package: the WebSocket control plane, the connection hub,
    the session clock and LiveKit token minting.
Why needed: Groups everything that keeps a live room in sync, separate from the media plane
    (LiveKit) and from AI evaluation, so a failure in either cannot stop the control plane.
Contains:
  - (no code) only this docstring; see hub.py, protocol.py, timer.py, livekit.py and ws.py.
Used by: nothing imports it directly (the package is imported implicitly by every
    services.realtime.* import)
Depends on: nothing inside the project (standard library / third-party only)
"""
