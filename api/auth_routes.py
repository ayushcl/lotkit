"""Small HTTP handlers for the invite-only owner authentication flow."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.base import BaseHTTPMiddleware

from api.auth import (
    GENERIC_LOGIN_DETAIL,
    MAX_PASSWORD_LENGTH,
    OWNER_CSRF_COOKIE,
    OWNER_SESSION_COOKIE,
    SESSION_LIFETIME_SECONDS,
    authenticate_credentials,
    create_session,
    current_owner_id,
    require_exact_origin,
    revoke_session,
)
from api.db import connect_db

router = APIRouter(prefix="/api/auth", tags=["authentication"])
LOGGER = logging.getLogger("uvicorn.error")
NO_STORE_HEADERS = {"Cache-Control": "no-store"}


class AuthNoStoreMiddleware(BaseHTTPMiddleware):
    """Prevent caching even for framework-generated auth error responses."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/auth/"):
            response.headers["Cache-Control"] = "no-store"
        return response


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    email: str = Field(max_length=320)
    password: str = Field(max_length=MAX_PASSWORD_LENGTH)


def _safe_user(user) -> dict[str, int | str | None]:
    return {
        "id": int(user["id"]),
        "email": str(user["email"]),
        "display_name": user["display_name"],
    }


def _set_auth_cookies(
    response: Response,
    request: Request,
    session,
) -> None:
    secure = request.app.state.settings.environment == "production"
    response.set_cookie(
        key=OWNER_SESSION_COOKIE,
        value=session.session_credential,
        max_age=SESSION_LIFETIME_SECONDS,
        path="/api",
        secure=secure,
        httponly=True,
        samesite="strict",
    )
    response.set_cookie(
        key=OWNER_CSRF_COOKIE,
        value=session.csrf_credential,
        max_age=SESSION_LIFETIME_SECONDS,
        path="/",
        secure=secure,
        httponly=False,
        samesite="strict",
    )


def _clear_auth_cookies(response: Response, request: Request) -> None:
    secure = request.app.state.settings.environment == "production"
    response.delete_cookie(
        OWNER_SESSION_COOKIE,
        path="/api",
        secure=secure,
        httponly=True,
        samesite="strict",
    )
    response.delete_cookie(
        OWNER_CSRF_COOKIE,
        path="/",
        secure=secure,
        httponly=False,
        samesite="strict",
    )


@router.post("/login")
def login(payload: LoginRequest, request: Request) -> JSONResponse:
    require_exact_origin(request)
    connection = connect_db()
    try:
        user = authenticate_credentials(
            connection,
            payload.email,
            payload.password,
        )
        if user is None:
            LOGGER.warning("Owner login failed.")
            return JSONResponse(
                status_code=401,
                content={"detail": GENERIC_LOGIN_DETAIL},
                headers=NO_STORE_HEADERS,
            )
        session = create_session(connection, int(user["id"]))
    finally:
        connection.close()

    response = JSONResponse(
        content=_safe_user(user),
        headers=NO_STORE_HEADERS,
    )
    _set_auth_cookies(response, request, session)
    LOGGER.info("Owner login succeeded: user_id=%s.", user["id"])
    return response


@router.get("/me")
def me(
    request: Request,
    owner_id: int = Depends(current_owner_id),
) -> JSONResponse:
    user = request.state.owner_user
    return JSONResponse(
        content={
            "id": owner_id,
            "email": user["email"],
            "display_name": user["display_name"],
        },
        headers=NO_STORE_HEADERS,
    )


@router.post("/logout", status_code=204)
def logout(
    request: Request,
    owner_id: int = Depends(current_owner_id),
) -> Response:
    connection = connect_db()
    try:
        revoke_session(connection, int(request.state.owner_session_id))
    finally:
        connection.close()
    response = Response(status_code=204, headers=NO_STORE_HEADERS)
    _clear_auth_cookies(response, request)
    LOGGER.info("Owner logout succeeded: user_id=%s.", owner_id)
    return response
