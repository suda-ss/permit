import hashlib
import hmac
import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import asyncpg
import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from tools.db import get_pool

SESSION_COOKIE = os.getenv("AUTH_SESSION_COOKIE", "permit_agent_session")
OAUTH_STATE_COOKIE = "permit_agent_oauth_state"
SESSION_DAYS = int(os.getenv("AUTH_SESSION_DAYS", "30"))
PBKDF2_ITERATIONS = 210_000


class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


def json(data: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status_code)


def _cookie_secure() -> bool:
    configured = os.getenv("AUTH_COOKIE_SECURE")
    if configured is not None:
        return configured.lower() in {"1", "true", "yes"}
    return os.getenv("NODE_ENV") == "production"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _session_expires_at() -> datetime:
    return datetime.now(UTC) + timedelta(days=SESSION_DAYS)


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PBKDF2_ITERATIONS,
    ).hex()
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str | None) -> bool:
    try:
        algorithm, iterations, salt, expected = str(stored or "").split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            int(iterations),
        ).hex()
        return hmac.compare_digest(digest, expected)
    except (TypeError, ValueError):
        return False


def public_user(row: asyncpg.Record | dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "email_verified": bool(row["email_verified_at"]),
    }


def _set_session_cookie(response: JSONResponse | RedirectResponse, token: str, expires: datetime) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        path="/",
        expires=expires,
    )


def _clear_session_cookie(response: JSONResponse | RedirectResponse) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


async def create_session_response(user_id: int, response: JSONResponse | RedirectResponse) -> None:
    token = secrets.token_urlsafe(48)
    expires_at = _session_expires_at()
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO auth_sessions (id, user_id, expires_at)
            VALUES ($1, $2, $3)
            """,
            _hash_token(token),
            user_id,
            expires_at,
        )
    _set_session_cookie(response, token, expires_at)


async def current_user(request: Request) -> asyncpg.Record | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None

    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT u.id, u.name, u.email, u.email_verified_at, u.created_at
              FROM auth_sessions s
              JOIN auth_users u ON u.id = s.user_id
             WHERE s.id = $1
               AND s.expires_at > now()
             LIMIT 1
            """,
            _hash_token(token),
        )


async def require_user(request: Request) -> asyncpg.Record:
    user = await current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


async def me(request: Request) -> JSONResponse:
    return json({"status": "SUCCESS", "user": public_user(await current_user(request))})


async def register(payload: RegisterRequest) -> JSONResponse:
    name = payload.name.strip()
    email = payload.email.strip().lower()
    password = payload.password

    if not name or not email or not password:
        return json({"status": "ERROR", "message": "Name, email, and password are required."}, 422)
    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        return json({"status": "ERROR", "message": "Enter a valid email address."}, 422)
    if len(password) < 8:
        return json({"status": "ERROR", "message": "Password must be at least 8 characters."}, 422)

    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            user = await conn.fetchrow(
                """
                INSERT INTO auth_users (name, email, password_hash)
                VALUES ($1, $2, $3)
                RETURNING id, name, email, email_verified_at, created_at
                """,
                name,
                email,
                hash_password(password),
            )
    except asyncpg.UniqueViolationError:
        return json({"status": "ERROR", "message": "An account already exists for this email."}, 409)

    response = json({"status": "SUCCESS", "user": public_user(user)})
    await create_session_response(user["id"], response)
    return response


async def login(payload: LoginRequest) -> JSONResponse:
    email = payload.email.strip().lower()
    pool = await get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            """
            SELECT id, name, email, password_hash, email_verified_at, created_at
              FROM auth_users
             WHERE lower(email) = lower($1)
             LIMIT 1
            """,
            email,
        )

    if not user or not verify_password(payload.password, user["password_hash"]):
        return json({"status": "ERROR", "message": "Invalid email or password."}, 401)

    response = json({"status": "SUCCESS", "user": public_user(user)})
    await create_session_response(user["id"], response)
    return response


async def logout(request: Request) -> JSONResponse:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM auth_sessions WHERE id = $1", _hash_token(token))
    response = json({"status": "SUCCESS"})
    _clear_session_cookie(response)
    return response


def _request_origin(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def _app_base_path() -> str:
    return os.getenv("APP_BASE_PATH", "/permits").rstrip("/")


def _app_base_url(request: Request) -> str:
    configured = os.getenv("AUTH_PUBLIC_URL")
    if configured:
        return configured.rstrip("/")
    return f"{_request_origin(request)}{_app_base_path()}"


def _safe_next(value: str | None) -> str:
    next_path = str(value or _app_base_path() or "/").strip() or "/"
    if not next_path.startswith("/") or next_path.startswith("//"):
        return _app_base_path() or "/"
    return next_path


def _redirect_with_error(request: Request, next_path: str, error: str) -> RedirectResponse:
    separator = "&" if "?" in next_path else "?"
    return RedirectResponse(
        f"{_request_origin(request)}{next_path}{separator}auth_error={error}",
        status_code=302,
    )


async def google_start(request: Request) -> RedirectResponse:
    client_id = os.getenv("GOOGLE_CLIENT_ID", "")
    next_path = _safe_next(request.query_params.get("next"))
    if not client_id:
        return _redirect_with_error(request, next_path, "google_not_configured")

    state = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(minutes=10)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM auth_oauth_states WHERE expires_at <= now()")
        await conn.execute(
            """
            INSERT INTO auth_oauth_states (id, next_path, expires_at)
            VALUES ($1, $2, $3)
            """,
            _hash_token(state),
            next_path,
            expires_at,
        )

    response = RedirectResponse(
        "https://accounts.google.com/o/oauth2/v2/auth?"
        + urlencode(
            {
                "client_id": client_id,
                "redirect_uri": f"{_app_base_url(request)}/api/auth/google/callback",
                "response_type": "code",
                "scope": "openid email profile",
                "state": state,
                "prompt": "select_account",
            }
        ),
        status_code=302,
    )
    response.set_cookie(
        OAUTH_STATE_COOKIE,
        state,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        path="/",
        max_age=10 * 60,
    )
    return response


async def google_callback(request: Request) -> RedirectResponse:
    code = request.query_params.get("code") or ""
    state = request.query_params.get("state") or ""
    expected_state = request.cookies.get(OAUTH_STATE_COOKIE) or ""
    next_path = _app_base_path() or "/"

    pool = await get_pool()
    async with pool.acquire() as conn:
        state_record = await conn.fetchrow(
            """
            DELETE FROM auth_oauth_states
             WHERE id = $1
               AND expires_at > now()
            RETURNING next_path
            """,
            _hash_token(state),
        )

    if state_record:
        next_path = _safe_next(state_record["next_path"])

    if not code or not state_record or not expected_state or not hmac.compare_digest(state, expected_state):
        response = _redirect_with_error(request, next_path, "google_state_mismatch")
        response.delete_cookie(OAUTH_STATE_COOKIE, path="/")
        return response

    client_id = os.getenv("GOOGLE_CLIENT_ID", "")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        response = _redirect_with_error(request, next_path, "google_not_configured")
        response.delete_cookie(OAUTH_STATE_COOKIE, path="/")
        return response

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            token_response = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": f"{_app_base_url(request)}/api/auth/google/callback",
                    "grant_type": "authorization_code",
                },
                headers={"Accept": "application/json"},
            )
            token_data = token_response.json()
            if token_response.status_code >= 400 or not token_data.get("access_token"):
                raise ValueError("Google token exchange failed")

            profile_response = await client.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={
                    "Authorization": f"Bearer {token_data['access_token']}",
                    "Accept": "application/json",
                },
            )
            profile = profile_response.json()
            email = str(profile.get("email") or "").strip().lower()
            if profile_response.status_code >= 400 or not email or profile.get("email_verified") is False:
                raise ValueError("Google profile did not include a verified email")
    except Exception:
        response = _redirect_with_error(request, next_path, "google_login_failed")
        response.delete_cookie(OAUTH_STATE_COOKIE, path="/")
        return response

    name = str(profile.get("name") or email.split("@", 1)[0] or "Permit Agent user").strip()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            """
            INSERT INTO auth_users (name, email, email_verified_at)
            VALUES ($1, $2, now())
            ON CONFLICT (email)
            DO UPDATE SET
                name = COALESCE(NULLIF(auth_users.name, ''), EXCLUDED.name),
                email_verified_at = COALESCE(auth_users.email_verified_at, now())
            RETURNING id, name, email, email_verified_at, created_at
            """,
            name,
            email,
        )

    response = RedirectResponse(f"{_request_origin(request)}{next_path}", status_code=302)
    response.delete_cookie(OAUTH_STATE_COOKIE, path="/")
    await create_session_response(user["id"], response)
    return response
