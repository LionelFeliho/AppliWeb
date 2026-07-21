from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Request, status


async def require_admin_api_key(
    request: Request, x_api_key: str | None = Header(default=None, alias="X-API-Key")
) -> None:
    configured = request.app.state.settings.admin_api_key
    if not configured:
        return
    if x_api_key is None or not hmac.compare_digest(x_api_key, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-API-Key",
        )
