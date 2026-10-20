from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

from connector_core.http import shared_client

from app.config import get_settings


@dataclass(frozen=True)
class OAuthProvider:
    name: str  # google | microsoft
    connector_provider: str  # gmail | outlook
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    client_id: str
    client_secret: str
    extra_auth_params: tuple[tuple[str, str], ...] = ()

    @property
    def redirect_uri(self) -> str:
        return f"{get_settings().public_web_url}/api/auth/{self.name}/callback"

    def authorization_url(self, state: str, code_challenge: str) -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            **dict(self.extra_auth_params),
        }
        return f"{self.authorize_url}?{urlencode(params)}"

    async def exchange_code(self, code: str, verifier: str) -> dict:
        resp = await shared_client().post(
            self.token_url,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": self.redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if resp.status_code >= 400:
            raise ValueError(f"{self.name} token exchange failed: {resp.text[:300]}")
        return resp.json()

    async def refresh(self, refresh_token: str) -> dict:
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        if self.name == "microsoft":
            data["scope"] = " ".join(self.scopes)
        resp = await shared_client().post(self.token_url, data=data)
        if resp.status_code >= 400:
            raise PermissionError(f"{self.name} refresh failed: {resp.text[:300]}")
        return resp.json()

    async def userinfo(self, access_token: str) -> dict:
        """Returns {subject, email, name}."""
        headers = {"Authorization": f"Bearer {access_token}"}
        if self.name == "google":
            resp = await shared_client().get("https://openidconnect.googleapis.com/v1/userinfo", headers=headers)
            resp.raise_for_status()
            d = resp.json()
            return {"subject": d["sub"], "email": d.get("email", ""), "name": d.get("name", "")}
        resp = await shared_client().get(
            "https://graph.microsoft.com/v1.0/me?$select=id,displayName,mail,userPrincipalName", headers=headers
        )
        resp.raise_for_status()
        d = resp.json()
        return {"subject": d["id"], "email": d.get("mail") or d.get("userPrincipalName", ""), "name": d.get("displayName", "")}


def get_provider(name: str) -> OAuthProvider:
    s = get_settings()
    if name == "google":
        return OAuthProvider(
            name="google",
            connector_provider="gmail",
            authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",
            scopes=(
                "openid",
                "email",
                "profile",
                "https://www.googleapis.com/auth/gmail.modify",
                "https://www.googleapis.com/auth/gmail.compose",
                "https://www.googleapis.com/auth/gmail.send",
                "https://www.googleapis.com/auth/gmail.settings.basic",
                "https://www.googleapis.com/auth/contacts.readonly",
                "https://www.googleapis.com/auth/calendar",
            ),
            client_id=s.google_client_id,
            client_secret=s.google_client_secret,
            extra_auth_params=(("access_type", "offline"), ("prompt", "consent"), ("include_granted_scopes", "true")),
        )
    if name == "microsoft":
        base = f"https://login.microsoftonline.com/{s.microsoft_tenant}/oauth2/v2.0"
        return OAuthProvider(
            name="microsoft",
            connector_provider="outlook",
            authorize_url=f"{base}/authorize",
            token_url=f"{base}/token",
            scopes=(
                "openid",
                "email",
                "profile",
                "offline_access",
                "User.Read",
                "Mail.ReadWrite",
                "Mail.Send",
                "MailboxSettings.ReadWrite",
                "Contacts.Read",
                "People.Read",
                "Calendars.ReadWrite",
            ),
            client_id=s.microsoft_client_id,
            client_secret=s.microsoft_client_secret,
            extra_auth_params=(("response_mode", "query"), ("prompt", "select_account")),
        )
    raise KeyError(name)
