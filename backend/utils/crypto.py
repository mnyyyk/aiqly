import os
from cryptography.fernet import Fernet

# ── .env / SecretsManager に保存しておく 32byte base64 ──
FERNET_KEY = os.environ["FERNET_KEY"].encode()

_fernet = Fernet(FERNET_KEY)

def encrypt_blob(data: bytes) -> bytes:
    """暗号化して bytes を返す"""
    return _fernet.encrypt(data)

def decrypt_blob(data: bytes) -> bytes:
    """復号して bytes を返す"""
    return _fernet.decrypt(data)