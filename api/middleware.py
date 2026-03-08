"""cocoro-agent — Authentication & Rate Limiting Middleware"""
from __future__ import annotations
import os
import logging
from fastapi import HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger("cocoro.agent.auth")

_bearer = HTTPBearer(auto_error=False)

COCORO_API_KEY: str = os.getenv("COCORO_API_KEY", "cocoro-dev-2026")


def verify_api_key(
    credentials: HTTPAuthorizationCredentials = Security(_bearer),
) -> str:
    """BearerトークンをAPIキーと照合して検証する依存関数"""
    if credentials is None or credentials.credentials != COCORO_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials.credentials
