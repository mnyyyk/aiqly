from flask import Blueprint, jsonify, current_app, request
from flask_login import login_required, current_user
import jwt, os
from datetime import datetime, timedelta

from backend.extensions import login_manager
from backend.models import User

jwt_bp = Blueprint("jwt_auth", __name__, url_prefix="/api")

@jwt_bp.get("/token")
@login_required
def issue_token():
    payload = {
        "user_id": current_user.id,
        "exp": datetime.utcnow() + timedelta(hours=12)
    }
    secret = current_app.config.get("JWT_SECRET_KEY") or os.getenv("JWT_SECRET_KEY", "")
    if not secret:
        return jsonify(status="error", message="JWT_SECRET_KEY not configured"), 500
    token = jwt.encode(payload, secret, algorithm="HS256")
    return jsonify(status="ok", token=token)


@login_manager.request_loader
def load_user_from_request(req):
    """
    Enable JWT Bearer authentication for Flask‑Login.
    Looks for `Authorization: Bearer <token>` header and returns a User.
    """
    auth = req.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None

    token = auth.split(None, 1)[1]
    secret = current_app.config.get("JWT_SECRET_KEY") or os.getenv("JWT_SECRET_KEY", "")
    if not secret:
        return None

    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None

    user_id = payload.get("user_id")
    if not user_id:
        return None

    return User.query.get(user_id)