"""cocoro-agent — Authentication & Rate Limiting Middleware"""
from __future__ import annotations
import os
import logging
from fastapi import HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger("cocoro.agent.auth")

_bearer = HTTPBearer(auto_error=False)


def verify_api_key(
    credentials: HTTPAuthorizationCredentials = Security(_bearer),
) -> str:
    """BearerトークンをAPIキーと照合して検証する依存関数
    
    NOTE: os.getenv() をリクエスト時に呼ぶことで、テスト時の環境変数セットが正しく反映される。
    """
    api_key = os.getenv("COCORO_API_KEY", "cocoro-dev-2026")
    if credentials is None or credentials.credentials != api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials.credentials
