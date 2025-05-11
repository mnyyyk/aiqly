# backend/extensions.py
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager

import json
from backend.utils.crypto import decrypt_blob

# インスタンスをここで作成
db = SQLAlchemy()
login_manager = LoginManager()


# --- Google Sites cookie helper -------------------------------------------------
def get_google_cookies(user_id: int):
    """
    Return a list[dict] ready for requests/Selenium from the encrypted
    google_cookies record for the given user_id, or None if not found/invalid.
    """
    # Late import to avoid circular dependency
    from backend.models import GoogleCookie

    rec = db.session.get(GoogleCookie, user_id)
    if not rec:
        return None

    try:
        raw_json = decrypt_blob(rec.cookie_json_encrypted)
        cookies = json.loads(raw_json)
        return cookies if isinstance(cookies, list) else None
    except Exception:
        # decryption or JSON error – treat as missing
        return None