# backend/extensions.py
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager

import json
import logging # ロガーを追加
from backend.utils.crypto import decrypt_blob # 修正: backend. からの相対インポート
# from backend.models import GoogleCookie # 関数内でインポートするためコメントアウト

# インスタンスをここで作成
db = SQLAlchemy()
login_manager = LoginManager()

logger = logging.getLogger(__name__) # ロガーインスタンスを作成

# --- Google Sites cookie helper -------------------------------------------------
def get_google_cookies(user_id: int) -> list[dict] | None: # 返り値の型ヒントを明示
    """
    Return a list[dict] ready for requests/Selenium from the encrypted
    google_cookies record for the given user_id, or None if not found/invalid.
    Assumes the JSON stored in DB is already processed to be Selenium-friendly.
    """
    # Late import to avoid circular dependency with models and db itself
    from backend.models import GoogleCookie
    # db はグローバルスコープのものを利用

    # GoogleCookieテーブルからuser_idでレコードを検索
    # scalars().first() を使うか、 get() が主キー検索なら get() を使う
    # GoogleCookie の主キーが user_id ではない場合 (複合主キーや別のid列がある場合) は filter_by を使う
    # ここでは GoogleCookie の主キーが user_id ではないと仮定し、filter_by を使用。
    # もし GoogleCookie の主キーが user_id なら db.session.get(GoogleCookie, user_id) でOK。
    # UserモデルとGoogleCookieモデルのリレーションが user_id を外部キーとしているため、
    # GoogleCookieテーブルには user_id 列があるはず。
    # GoogleCookieテーブルの主キーが 'id' で、user_id は単なるカラムの場合:
    rec = db.session.scalars(
        db.select(GoogleCookie).filter_by(user_id=user_id)
    ).first()


    if not rec:
        logger.info(f"No GoogleCookie record found for user_id: {user_id}")
        return None

    try:
        raw_json_bytes = decrypt_blob(rec.cookie_json_encrypted)
        raw_json_str = raw_json_bytes.decode('utf-8')
        # デバッグレベルをINFOに上げるか、必要な時だけ出力するように調整
        logger.debug(f"Decrypted raw_json_str for user {user_id} (first 500 chars): {raw_json_str[:500]}")

        cookies_from_db = json.loads(raw_json_str)

        if not isinstance(cookies_from_db, list):
            logger.error(f"Decrypted cookie data for user {user_id} is not a list: {type(cookies_from_db)}")
            return None

        # DBに保存されるJSONは既にSeleniumに適した形式になっているはずなので、
        # ここでの変換処理は最小限のバリデーションや微調整に留める。
        # main.pyの/api/google/upload_cookiesで整形済み。
        
        valid_selenium_cookies = []
        for ck in cookies_from_db:
            if isinstance(ck, dict) and ck.get("name") and ck.get("value") and ck.get("domain"):
                # expiry が存在し、かつ数値でない場合は警告 (main.pyで数値化しているはず)
                if "expiry" in ck and ck["expiry"] is not None and not isinstance(ck["expiry"], (int, float)):
                    logger.warning(f"Cookie '{ck['name']}' for user {user_id} has non-numeric expiry: {ck['expiry']}. Skipping expiry.")
                    # expiryを削除するか、Noneにするか、エラーとするか、main.py側の処理を信じるか
                    # ここでは、main.pyで処理されていると信じ、そのまま通すか、必要なら型チェック
                    try:
                        ck["expiry"] = int(float(ck["expiry"])) # 再度数値化を試みる
                    except (ValueError, TypeError):
                         logger.error(f"Could not convert expiry to int for cookie {ck.get('name')} in get_google_cookies. Value: {ck['expiry']}")
                         ck.pop("expiry", None) # 問題のあるexpiryは削除

                valid_selenium_cookies.append(ck)
            else:
                logger.warning(f"Skipping malformed cookie from DB for user {user_id}: {ck}")
        
        if not valid_selenium_cookies and cookies_from_db: # 元のリストは空でなかったのに、有効なものが0になった場合
             logger.error(f"No valid Selenium-compatible cookies found for user {user_id} after parsing DB data.")
             return None

        logger.info(f"Successfully retrieved and validated {len(valid_selenium_cookies)} cookies for user {user_id}.")
        if valid_selenium_cookies:
            logger.debug(f"Sample of first validated Selenium cookie for user {user_id}: {str(valid_selenium_cookies[0])[:300]}")

        # extra summary log
        try:
            logger.info("get_google_cookies: user %s → %d cookies → %s",
                        user_id,
                        len(valid_selenium_cookies),
                        [c.get("name") for c in valid_selenium_cookies][:10])
        except Exception:
            logger.debug("get_google_cookies: logging failed", exc_info=True)

        return valid_selenium_cookies

    except json.JSONDecodeError as e:
        logger.error(f"JSONDecodeError when parsing cookies for user {user_id}: {e}. Raw data (first 500): {raw_json_str[:500] if 'raw_json_str' in locals() else 'N/A'}", exc_info=True)
        return None
    except Exception as e:
        logger.error(f"Generic error processing cookies for user {user_id} in get_google_cookies: {e}", exc_info=True)
        return None