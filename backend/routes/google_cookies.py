from flask import Blueprint, request, jsonify, abort
from flask_login import login_required, current_user

from backend.models import db, GoogleCookie
from backend.utils.crypto import encrypt_blob

bp = Blueprint("google_cookies", __name__, url_prefix="/api")

@bp.post("/google-cookies")
@login_required
def save_google_cookies():
    payload = request.get_json(force=True, silent=True) or {}
    if "cookie_json" not in payload:
        abort(400, description="cookie_json missing")

    encrypted = encrypt_blob(payload["cookie_json"].encode())
    gc = GoogleCookie(user_id=current_user.id,
                      cookie_json_encrypted=encrypted)
    db.session.add(gc)
    db.session.commit()
    return jsonify(status="ok"), 201