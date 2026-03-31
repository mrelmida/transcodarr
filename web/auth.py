# web/auth.py
# FastAPI dependency for API key authentication
from fastapi import Request, Header, Query, HTTPException


async def require_api_key(
    request: Request,
    x_api_key: str = Header(None, alias="X-API-Key"),
    api_key: str = Query(None),
):
    s = request.app.state.settings
    key = x_api_key or api_key
    if s.ADMIN_API_KEY and key == s.ADMIN_API_KEY:
        return True
    raise HTTPException(status_code=401, detail="Unauthorized")
