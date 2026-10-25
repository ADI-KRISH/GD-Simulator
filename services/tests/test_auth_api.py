"""End-to-end tests for the auth slice (FR-01, FR-02).

Purpose: End-to-end tests of the auth HTTP API: registration, login, token refresh and rotation,
    logout, password reset and profile updates.
Why needed: Authentication guards every other endpoint, so these tests pin down the security
    rules: no account enumeration, single-use reset tokens, refresh-token rotation and
    revocation, and refresh tokens that cannot act as access tokens.
Contains:
  - Registration tests: new user with an empty profile, duplicate and case-variant emails, short
    passwords.
  - Login tests: token pair, wrong password and unknown email (both 401).
  - Token tests: /me needs a token and refuses a refresh token; refresh rotates and invalidates
    the old token; logout revokes.
  - Password reset tests: full flow, no reuse of a token, no leak for unknown emails.
  - Profile tests: update, require auth, reject invalid experience level.
Used by: nothing imports it directly (run by pytest, which discovers test_*.py files)
Depends on: nothing inside the project (standard library / third-party only)
"""

from httpx import AsyncClient


async def test_register_creates_user_with_empty_profile(client: AsyncClient, credentials: dict):
    """Registering returns a new active candidate with an empty profile."""
    # Register through the real HTTP route, using the credentials fixture.
    response = await client.post("/api/v1/auth/register", json=credentials)
    assert response.status_code == 201, response.text

    # Check the shape of the response, including what must NOT be there.
    body = response.json()
    assert body["email"] == credentials["email"]
    assert body["role"] == "candidate"
    assert body["is_active"] is True
    assert body["profile"]["experience_level"] == "student"
    assert body["profile"]["sessions_completed"] == 0
    # The password and its hash must never be returned to the client.
    assert "password" not in body and "password_hash" not in body


async def test_register_rejects_duplicate_email(client: AsyncClient, registered: dict, credentials):
    """A second registration with the same email is a 409 conflict."""
    # Register the same email again; the registered fixture already created the first account.
    response = await client.post("/api/v1/auth/register", json=credentials)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


async def test_register_normalises_email_case(client: AsyncClient, credentials: dict):
    """Emails differing only by case count as duplicates."""
    # First registration succeeds.
    await client.post("/api/v1/auth/register", json=credentials)
    # The same email in upper case must still collide.
    shouting = {**credentials, "email": credentials["email"].upper()}
    assert (await client.post("/api/v1/auth/register", json=shouting)).status_code == 409


async def test_register_rejects_short_password(client: AsyncClient, credentials: dict):
    """A too-short password is rejected with 422."""
    # A password below the minimum length.
    response = await client.post("/api/v1/auth/register", json={**credentials, "password": "short"})
    assert response.status_code == 422


async def test_login_returns_token_pair(client: AsyncClient, tokens: dict):
    """Login returns distinct bearer access and refresh tokens."""
    # The tokens fixture comes from a real login.
    assert tokens["token_type"] == "bearer"
    assert tokens["access_token"] and tokens["refresh_token"]
    assert tokens["access_token"] != tokens["refresh_token"]


async def test_login_with_wrong_password_is_rejected(client: AsyncClient, registered, credentials):
    """A wrong password gives 401."""
    # Right email, wrong password.
    response = await client.post(
        "/api/v1/auth/login", json={"email": credentials["email"], "password": "not-it"}
    )
    assert response.status_code == 401


async def test_login_with_unknown_email_is_rejected(client: AsyncClient):
    """An unknown email gives 401."""
    # An email that was never registered must fail exactly like a wrong password.
    response = await client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": "whatever-123"}
    )
    assert response.status_code == 401


async def test_me_requires_a_token(client: AsyncClient):
    """/auth/me without a token is 401."""
    # No Authorization header at all.
    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_me_rejects_a_refresh_token(client: AsyncClient, tokens: dict):
    """A refresh token cannot be used as an access token."""
    # Present a refresh token where an access token is expected.
    response = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {tokens['refresh_token']}"}
    )
    assert response.status_code == 401


async def test_me_returns_the_authenticated_user(
    client: AsyncClient, auth_header: dict, credentials: dict
):
    """/auth/me returns the signed-in user."""
    # The auth_header fixture carries a valid access token.
    response = await client.get("/api/v1/auth/me", headers=auth_header)
    assert response.status_code == 200
    assert response.json()["email"] == credentials["email"]


async def test_refresh_rotates_and_invalidates_the_old_token(client: AsyncClient, tokens: dict):
    """Refreshing issues a new token and the old one stops working."""
    # The first refresh succeeds and hands out a different refresh token.
    first = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert first.status_code == 200
    rotated = first.json()
    assert rotated["refresh_token"] != tokens["refresh_token"]

    # Replaying the old refresh token must now fail (rotation).
    replay = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert replay.status_code == 401

    # The newly issued access token still works.
    still_valid = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {rotated['access_token']}"}
    )
    assert still_valid.status_code == 200


async def test_logout_revokes_the_refresh_token(client: AsyncClient, tokens: dict):
    """After logout the refresh token is rejected."""
    # Logout revokes the refresh token.
    logout = await client.post(
        "/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}
    )
    assert logout.status_code == 204

    # Using the revoked token to refresh must fail.
    response = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert response.status_code == 401


async def test_logout_with_a_junk_token_still_succeeds(client: AsyncClient):
    """Logging out with an invalid token still returns 204."""
    # A garbage token is already logged out, so logout still answers 204.
    response = await client.post("/api/v1/auth/logout", json={"refresh_token": "not-a-jwt"})
    assert response.status_code == 204


async def test_password_reset_flow(client: AsyncClient, tokens: dict, credentials: dict):
    """Request a reset token, set a new password, and sign in with it."""
    # Step 1: ask for a reset token (returned directly in local mode).
    issued = await client.post(
        "/api/v1/auth/password-reset/request", json={"email": credentials["email"]}
    )
    assert issued.status_code == 200
    reset_token = issued.json()["reset_token"]
    assert reset_token

    # Step 2: set a new password using that token.
    confirmed = await client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": reset_token, "new_password": "a-brand-new-password"},
    )
    assert confirmed.status_code == 204

    # The old password no longer works.
    old_login = await client.post(
        "/api/v1/auth/login",
        json={"email": credentials["email"], "password": credentials["password"]},
    )
    assert old_login.status_code == 401

    # The new password works.
    new_login = await client.post(
        "/api/v1/auth/login",
        json={"email": credentials["email"], "password": "a-brand-new-password"},
    )
    assert new_login.status_code == 200

    # Changing the password must kill sessions issued before the reset.
    stale = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert stale.status_code == 401


async def test_reset_token_cannot_be_reused(client: AsyncClient, registered, credentials: dict):
    """A reset token works once; a second use fails."""
    # Get a reset token.
    issued = await client.post(
        "/api/v1/auth/password-reset/request", json={"email": credentials["email"]}
    )
    reset_token = issued.json()["reset_token"]

    # The first use succeeds.
    first = await client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": reset_token, "new_password": "first-new-password"},
    )
    assert first.status_code == 204

    # The same token again must be rejected (single use).
    second = await client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": reset_token, "new_password": "second-new-password"},
    )
    assert second.status_code == 401


async def test_reset_request_for_unknown_email_does_not_leak(client: AsyncClient):
    """Requesting a reset for an unknown email looks the same but returns no token."""
    # Unknown email: the same 200 reply but no token, so accounts cannot be enumerated.
    response = await client.post(
        "/api/v1/auth/password-reset/request", json={"email": "nobody@example.com"}
    )
    assert response.status_code == 200
    assert response.json()["reset_token"] is None


async def test_profile_update(client: AsyncClient, auth_header: dict):
    """PATCH /auth/me updates skills and experience level."""
    # Change skills and experience level.
    response = await client.patch(
        "/api/v1/auth/me",
        headers=auth_header,
        json={"skills": ["public speaking", "systems design"], "experience_level": "mid"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["profile"]["skills"] == ["public speaking", "systems design"]
    assert body["profile"]["experience_level"] == "mid"

    # Read back to prove the change was saved, not just echoed.
    persisted = await client.get("/api/v1/auth/me", headers=auth_header)
    assert persisted.json()["profile"]["skills"] == ["public speaking", "systems design"]


async def test_profile_update_requires_auth(client: AsyncClient):
    """Updating the profile without a token is 401."""
    # No token supplied.
    response = await client.patch("/api/v1/auth/me", json={"skills": []})
    assert response.status_code == 401


async def test_profile_update_rejects_unknown_experience_level(
    client: AsyncClient, auth_header: dict
):
    """An invalid experience level is rejected with 422."""
    # wizard is not a valid experience level.
    response = await client.patch(
        "/api/v1/auth/me", headers=auth_header, json={"experience_level": "wizard"}
    )
    assert response.status_code == 422
