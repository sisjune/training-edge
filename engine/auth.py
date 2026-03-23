"""API key authentication for TrainingEdge."""
import secrets
from fastapi import Request, HTTPException, Depends
from engine import database


def get_or_create_api_key() -> str:
    """Get existing API key or generate a new one."""
    database.init_db()
    with database.get_db() as conn:
        key = database.get_setting(conn, 'api_key')
        if not key:
            key = secrets.token_urlsafe(32)
            database.set_setting(conn, 'api_key', key)
    return key


async def verify_api_key(request: Request):
    """FastAPI dependency to verify API key.

    Checks X-API-Key header or api_key query param.
    Skips auth if no API key is configured (dev mode).
    """
    database.init_db()
    with database.get_db() as conn:
        stored_key = database.get_setting(conn, 'api_key')

    if not stored_key:
        return  # No key set = dev mode, skip auth

    # Check header first, then query param
    provided = request.headers.get('X-API-Key') or request.query_params.get('api_key')

    if not provided:
        raise HTTPException(status_code=401, detail='API key required. Use X-API-Key header or api_key query param.')

    if provided != stored_key:
        raise HTTPException(status_code=403, detail='Invalid API key.')
