"""End-to-end tests for the auth slice (FR-01, FR-02)."""

from httpx import AsyncClient


async def test_register_creates_user_with_empty_profile(client: AsyncClient, credentials: dict):
    response = await client.post("/api/v1/auth/register", json=credentials)
    assert response.status_code == 201, response.text

    body = response.json()
    assert body["email"] == credentials["email"]
    assert body["role"] == "candidate"
    assert body["is_active"] is True
    assert body["profile"]["experience_level"] == "student"
    assert body["profile"]["sessions_completed"] == 0
    assert "password" not in body and "password_hash" not in body


async def test_register_rejects_duplicate_email(client: AsyncClient, registered: dict, credentials):
    response = await client.post("/api/v1/auth/register", json=credentials)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


async def test_register_normalises_email_case(client: AsyncClient, credentials: dict):
    await client.post("/api/v1/auth/register", json=credentials)
    shouting = {**credentials, "email": credentials["email"].upper()}
    assert (await client.post("/api/v1/auth/register", json=shouting)).status_code == 409


async def test_register_rejects_short_password(client: AsyncClient, credentials: dict):
    response = await client.post("/api/v1/auth/register", json={**credentials, "password": "short"})
    assert response.status_code == 422


async def test_login_returns_token_pair(client: AsyncClient, tokens: dict):
    assert tokens["token_type"] == "bearer"
    assert tokens["access_token"] and tokens["refresh_token"]
    assert tokens["access_token"] != tokens["refresh_token"]


async def test_login_with_wrong_password_is_rejected(client: AsyncClient, registered, credentials):
    response = await client.post(
        "/api/v1/auth/login", json={"email": credentials["email"], "password": "not-it"}
    )
    assert response.status_code == 401


async def test_login_with_unknown_email_is_rejected(client: AsyncClient):
    response = await client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": "whatever-123"}
    )
    assert response.status_code == 401


async def test_me_requires_a_token(client: AsyncClient):
    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_me_rejects_a_refresh_token(client: AsyncClient, tokens: dict):
    response = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {tokens['refresh_token']}"}
    )
    assert response.status_code == 401


async def test_me_returns_the_authenticated_user(
    client: AsyncClient, auth_header: dict, credentials: dict
):
    response = await client.get("/api/v1/auth/me", headers=auth_header)
    assert response.status_code == 200
    assert response.json()["email"] == credentials["email"]


async def test_refresh_rotates_and_invalidates_the_old_token(client: AsyncClient, tokens: dict):
    first = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert first.status_code == 200
    rotated = first.json()
    assert rotated["refresh_token"] != tokens["refresh_token"]

    replay = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert replay.status_code == 401

    still_valid = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {rotated['access_token']}"}
    )
    assert still_valid.status_code == 200


async def test_logout_revokes_the_refresh_token(client: AsyncClient, tokens: dict):
    logout = await client.post(
        "/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}
    )
    assert logout.status_code == 204

    response = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert response.status_code == 401


async def test_logout_with_a_junk_token_still_succeeds(client: AsyncClient):
    response = await client.post("/api/v1/auth/logout", json={"refresh_token": "not-a-jwt"})
    assert response.status_code == 204


async def test_password_reset_flow(client: AsyncClient, tokens: dict, credentials: dict):
    issued = await client.post(
        "/api/v1/auth/password-reset/request", json={"email": credentials["email"]}
    )
    assert issued.status_code == 200
    reset_token = issued.json()["reset_token"]
    assert reset_token

    confirmed = await client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": reset_token, "new_password": "a-brand-new-password"},
    )
    assert confirmed.status_code == 204

    old_login = await client.post(
        "/api/v1/auth/login",
        json={"email": credentials["email"], "password": credentials["password"]},
    )
    assert old_login.status_code == 401

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
    issued = await client.post(
        "/api/v1/auth/password-reset/request", json={"email": credentials["email"]}
    )
    reset_token = issued.json()["reset_token"]

    first = await client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": reset_token, "new_password": "first-new-password"},
    )
    assert first.status_code == 204

    second = await client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": reset_token, "new_password": "second-new-password"},
    )
    assert second.status_code == 401


async def test_reset_request_for_unknown_email_does_not_leak(client: AsyncClient):
    response = await client.post(
        "/api/v1/auth/password-reset/request", json={"email": "nobody@example.com"}
    )
    assert response.status_code == 200
    assert response.json()["reset_token"] is None


async def test_profile_update(client: AsyncClient, auth_header: dict):
    response = await client.patch(
        "/api/v1/auth/me",
        headers=auth_header,
        json={"skills": ["public speaking", "systems design"], "experience_level": "mid"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["profile"]["skills"] == ["public speaking", "systems design"]
    assert body["profile"]["experience_level"] == "mid"

    persisted = await client.get("/api/v1/auth/me", headers=auth_header)
    assert persisted.json()["profile"]["skills"] == ["public speaking", "systems design"]


async def test_profile_update_requires_auth(client: AsyncClient):
    response = await client.patch("/api/v1/auth/me", json={"skills": []})
    assert response.status_code == 401


async def test_profile_update_rejects_unknown_experience_level(
    client: AsyncClient, auth_header: dict
):
    response = await client.patch(
        "/api/v1/auth/me", headers=auth_header, json={"experience_level": "wizard"}
    )
    assert response.status_code == 422
