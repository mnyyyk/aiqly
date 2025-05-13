# backend/routes/auth_jwt.py
from flask import Blueprint, jsonify
from flask_login import login_required, current_user
import jwt, os
from datetime import datetime, timedelta

jwt_bp = Blueprint("jwt_auth", __name__, url_prefix="/api")

@jwt_bp.get("/token")
@login_required
def issue_token():
    payload = {
        "user_id": current_user.id,
        "exp": datetime.utcnow() + timedelta(hours=12)
    }
    secret = os.getenv("JWT_SECRET_KEY")
    token  = jwt.encode(payload, secret, algorithm="HS256")
    return jsonify(status="ok", token=token)