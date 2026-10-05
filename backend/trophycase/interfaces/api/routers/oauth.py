"""OAuth2 (Google) sign-in.

Flow: ``GET /v1/auth/oauth/google/authorize`` returns the consent URL; Google redirects to
``/v1/auth/oauth/google/callback`` with a one-time ``code``; we exchange it server-side
(keeping the client secret private) and issue our own JWT pair.

Every outbound HTTPS call uses :attr:`Settings.tls_verify`, so a corporate CA can be
supplied through ``TROPHYCASE_CA_BUNDLE`` - verification is never disabled.
"""

from __future__ import annotations

from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Query

from trophycase.application.auth_service import AuthService
from trophycase.config import get_settings
from trophycase.domain.errors import ValidationError
from trophycase.interfaces.api.deps import DbDep
from trophycase.interfaces.api.routers.auth import to_auth_out
from trophycase.interfaces.api.schemas import AuthOut, MessageOut

router = APIRouter(prefix="/v1/auth/oauth", tags=["oauth"])

_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
_SUPPORTED_PROVIDERS = frozenset({"google"})


def _require_google() -> tuple[str, str, str]:
    settings = get_settings()
    client_id = settings.oauth_google_client_id
    client_secret = settings.oauth_google_client_secret.get_secret_value()
    if not client_id or not client_secret:
        raise ValidationError(
            "google sign-in is not configured",
            details={"hint": "set TROPHYCASE_OAUTH_GOOGLE_CLIENT_ID and _SECRET"},
        )
    return client_id, client_secret, settings.oauth_redirect_uri


@router.get("/{provider}/authorize")
async def authorize(provider: str) -> MessageOut:
    """Return the URL the user should be sent to for consent."""
    if provider not in _SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=404, detail="unsupported oauth provider")
    client_id, _secret, redirect_uri = _require_google()
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    return MessageOut(message=f"{_GOOGLE_AUTH_URL}?{query}")


@router.get("/{provider}/callback", response_model=AuthOut)
async def callback(
    provider: str,
    db: DbDep,
    code: str = Query(..., min_length=1),
    state: str = Query(default="default", max_length=128),
) -> AuthOut:
    """Exchange the authorization code and log the user in."""
    if provider not in _SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=404, detail="unsupported oauth provider")
    client_id, client_secret, redirect_uri = _require_google()

    try:
        async with httpx.AsyncClient(
            verify=get_settings().tls_verify, timeout=15.0, follow_redirects=False
        ) as client:
            token_response = await client.post(
                _GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
                headers={"Accept": "application/json"},
            )
            if token_response.status_code != 200:
                raise ValidationError(
                    "oauth code exchange failed",
                    details={"status": token_response.status_code},
                )
            access_token = token_response.json().get("access_token")
            if not isinstance(access_token, str):
                raise ValidationError("oauth provider did not return an access token")
            userinfo = await client.get(
                _GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if userinfo.status_code != 200:
                raise ValidationError(
                    "could not read oauth profile", details={"status": userinfo.status_code}
                )
            profile = userinfo.json()
    except httpx.HTTPError as exc:  # TLS/network failures surface here, never as verify=False
        raise ValidationError(f"oauth request failed: {exc}") from exc

    subject = str(profile.get("sub", ""))
    email = str(profile.get("email", ""))
    if not subject or not email:
        raise ValidationError("oauth profile is missing sub/email")

    service = AuthService(db)
    result = await service.oauth_login(
        provider=provider,
        subject=subject,
        email=email,
        display_name=str(profile.get("name") or email.split("@")[0]),
        avatar_url=str(profile["picture"]) if profile.get("picture") else None,
        device_id=state[:64] or "default",
        device_name="oauth device",
    )
    return to_auth_out(result)
