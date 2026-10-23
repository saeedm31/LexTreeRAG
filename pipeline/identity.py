"""
Who is using the app, read from the login layer in front of it.

On AWS the load balancer signs users in (Amazon Cognito, which can connect to a company's
Microsoft / Google / Okta login) and passes the user to the app in the `x-amzn-oidc-data`
header (a signed JWT). The app only uses it to file spend under the right person and team.
The task accepts traffic from the load balancer only (security group), so the header cannot
come from anywhere else. Without login (IP-restricted test setup) everyone is "anonymous".
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass


@dataclass
class User:
    id: str
    email: str | None
    team: str


def _claims(jwt: str) -> dict:
    try:
        payload = jwt.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except Exception:
        return {}


def current_user(headers) -> User:
    """The signed-in user from the request headers (case-insensitive mapping), or 'anonymous'."""
    default_team = os.getenv("DEFAULT_TEAM", "default")
    get = (lambda k: headers.get(k) or headers.get(k.title())) if headers else (lambda k: None)
    claims = _claims(get("x-amzn-oidc-data") or "")
    uid = claims.get("sub") or get("x-amzn-oidc-identity")
    if not uid:
        return User("anonymous", None, default_team)
    team = claims.get("custom:team") or (claims.get("cognito:groups") or [None])[0] or default_team
    return User(str(uid), claims.get("email"), str(team))
