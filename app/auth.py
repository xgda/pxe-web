"""极简令牌鉴权：HMAC 签名 + 过期时间，避免额外依赖。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from fastapi import Depends, Header, HTTPException

import config


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(payload: str) -> str:
    return hmac.new(config.SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()


def create_token(username: str) -> str:
    body = {"u": username, "exp": int(time.time()) + config.TOKEN_TTL}
    payload = _b64(json.dumps(body, separators=(",", ":")).encode())
    return f"{payload}.{_sign(payload)}"


def verify_token(token: str | None) -> str | None:
    if not token or "." not in token:
        return None
    payload, _, sig = token.rpartition(".")
    if not hmac.compare_digest(_sign(payload), sig):
        return None
    try:
        body = json.loads(_unb64(payload))
    except Exception:
        return None
    if int(body.get("exp", 0)) < time.time():
        return None
    return body.get("u")


def require_auth(authorization: str | None = Header(default=None)) -> str:
    """从 Authorization: Bearer <token> 中取令牌。"""
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    user = verify_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录")
    return user
