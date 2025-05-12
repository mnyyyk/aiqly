"""
Google Site / other service cookies upload endpoint (JWT‑auth + multipart or JSON).

Accepts either:
  • multipart/form-data with a file field named "file" containing raw cookie JSON
  • application/json body with {"cookie_json": "..."}  (legacy)

Authentication:
  Bearer JWT in `Authorization` header.
  The token is validated with app.config["JWT_SECRET_KEY"]; claim **sub** must contain user id.
"""

import jwt
from flask import Blueprint, request, jsonify, abort, current_app
from backend.models import db, GoogleCookie, User
from backend.utils.crypto import encrypt_blob

bp = Blueprint("google_cookies", __name__)
google_cookies_bp = bp


def _authenticate_bearer(req) -> User | None:
    """Return User from Bearer token; None if invalid / missing."""
    auth = req.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth.split(None, 1)[1]
    try:
        payload = jwt.decode(
            token,
            current_app.config.get("JWT_SECRET_KEY", ""),
            algorithms=["HS256"],
        )
    except jwt.PyJWTError:
        return None

    user_id = payload.get("sub")
    if not user_id:
        return None
    return db.session.get(User, int(user_id))


@bp.post("/google-cookies")
def save_google_cookies():
    # --- Auth ----
    user = _authenticate_bearer(request)
    if not user:
        abort(401, description="Unauthorized")

    # --- Payload ----
    raw_bytes: bytes | None = None

    if "file" in request.files:
        raw_bytes = request.files["file"].read()
    else:
        payload = request.get_json(force=True, silent=True) or {}
        cookie_json = payload.get("cookie_json")
        if cookie_json:
            raw_bytes = cookie_json.encode()

    if not raw_bytes:
        abort(400, description="cookie data missing")

    # --- Encrypt & persist ----
    encrypted = encrypt_blob(raw_bytes)
    gc = GoogleCookie(user_id=user.id, cookie_json_encrypted=encrypted)
    db.session.add(gc)
    db.session.commit()
    return jsonify(status="ok"), 201