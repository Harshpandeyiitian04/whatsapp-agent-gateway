import hmac

from fastapi import HTTPException, Request


def require_admin(request: Request) -> None:
    expected = request.app.state.app_state.settings.admin_token
    header = request.headers.get("authorization", "")
    token = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="admin token required")
