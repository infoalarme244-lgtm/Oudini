"""Mots de passe, sessions signées, CSRF, chiffrement des secrets marchands."""
import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, URLSafeTimedSerializer

from ..config import settings

_serializer = URLSafeTimedSerializer(settings.secret_key, salt="shopforge-session")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or not stored.startswith("scrypt$"):
        return False
    _, salt_b64, dk_b64 = stored.split("$")
    dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(dk, base64.b64decode(dk_b64))


def sign_session(data: dict) -> str:
    return _serializer.dumps(data)


def read_session(value: str | None, max_age: int = 14 * 24 * 3600) -> dict:
    if not value:
        return {}
    try:
        return _serializer.loads(value, max_age=max_age)
    except BadSignature:
        return {}


def csrf_token_for(session_id: str) -> str:
    return hmac.new(settings.secret_key.encode(), f"csrf:{session_id}".encode(), hashlib.sha256).hexdigest()


def _fernet() -> Fernet:
    key = hashlib.sha256(("fernet:" + settings.secret_key).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode() if value else ""


def decrypt_secret(value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        return ""


def mask(value: str) -> str:
    return (value[:7] + "…" + value[-4:]) if value and len(value) > 12 else "••••"
