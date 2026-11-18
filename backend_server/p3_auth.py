"""P3 (SV3): local JWT identity/RBAC for the FastAPI dashboard.

The ESP32 keeps its independent P1 device key. No default credentials or
signing keys are shipped. HTTPS must terminate at a trusted reverse proxy
before this service is exposed beyond localhost.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from fastapi import HTTPException

ISSUER = "iot-waste-management"
AUDIENCE = "iot-waste-dashboard"
TOKEN_LIFETIME_SECONDS = 30 * 60
PBKDF2_ROUNDS = 600_000
ROLES = {"viewer", "operator", "admin"}
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
AUTH_FILE = Path(os.getenv("IOT_P3_AUTH_FILE", Path(__file__).with_name("p3_local_auth.json")))


def init_auth_tables(db_file: str) -> None:
    """Only additive changes: existing bins/logs/priority never change."""
    with closing(sqlite3.connect(db_file)) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS dashboard_users (
            username TEXT PRIMARY KEY,
            password_salt TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            password_iterations INTEGER NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('viewer','operator','admin')),
            disabled INTEGER NOT NULL DEFAULT 0,
            token_version INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        )""")
        db.commit()


def bootstrap_signing_secret(auth_file: Path = AUTH_FILE) -> None:
    """Called explicitly by setup CLI, never implicitly by the web server."""
    auth_file = Path(auth_file)
    auth_file.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"jwt_secret": secrets.token_urlsafe(48)}, indent=2)
    try:
        fd = os.open(str(auth_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return  # Never replace the secret: existing sessions must not break.
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(payload + "\n")


def _signing_secret() -> str:
    try:
        config = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
        secret = config["jwt_secret"]
        if not isinstance(secret, str) or len(secret) < 48:
            raise ValueError("weak secret")
        return secret
    except (OSError, ValueError, KeyError, TypeError):
        raise HTTPException(status_code=503, detail="JWT not configured; run p3_setup.py first")


def _hash_password(password: str, salt: bytes, iterations: int) -> str:
    return base64.b64encode(hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)).decode("ascii")


def add_user(db_file: str, username: str, password: str, role: str = "viewer") -> None:
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("Username must have 3-32 letters, numbers, dots, dashes or underscores")
    if role not in ROLES:
        raise ValueError("Role must be viewer, operator or admin")
    if not (12 <= len(password) <= 256):
        raise ValueError("Password must contain 12-256 characters")
    salt = secrets.token_bytes(16)
    init_auth_tables(db_file)
    with closing(sqlite3.connect(db_file)) as db:
        db.execute("""INSERT INTO dashboard_users
            (username, password_salt, password_hash, password_iterations, role, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (username, base64.b64encode(salt).decode("ascii"),
             _hash_password(password, salt, PBKDF2_ROUNDS), PBKDF2_ROUNDS, role, time.time()))
        db.commit()


def authenticate(db_file: str, username: str, password: str) -> dict:
    """Generic errors; never echo whether a username exists."""
    with closing(sqlite3.connect(db_file)) as db:
        row = db.execute("""SELECT username, password_salt, password_hash,
            password_iterations, role, disabled, token_version
            FROM dashboard_users WHERE username=?""", (username,)).fetchone()
    if row:
        # Validate stored iterations to avoid any corrupt DB record causing work amplification.
        rounds = row[3]
        if not isinstance(rounds, int) or rounds < 100_000 or rounds > 1_000_000:
            raise HTTPException(status_code=401, detail="Invalid username or password")
        try:
            salt = base64.b64decode(row[1], validate=True)
            expected = _hash_password(password, salt, rounds)
        except (ValueError, TypeError):
            expected = ""
        if not row[5] and hmac.compare_digest(expected, row[2]):
            return {"username": row[0], "role": row[4], "token_version": row[6]}
    else:
        # Equalize cost for unknown usernames (per-process DoS/rate limits still needed in production).
        _hash_password(password, b"\x00" * 16, PBKDF2_ROUNDS)
    raise HTTPException(status_code=401, detail="Invalid username or password")


def issue_token(identity: dict) -> str:
    try:
        import jwt  # PyJWT; see requirements_p3.txt
    except ImportError:
        raise HTTPException(status_code=503, detail="PyJWT missing; install requirements_p3.txt")
    now = int(time.time())
    return jwt.encode({
        "sub": identity["username"], "role": identity["role"],
        "ver": identity["token_version"], "iat": now, "nbf": now,
        "exp": now + TOKEN_LIFETIME_SECONDS, "iss": ISSUER,
        "aud": AUDIENCE, "jti": secrets.token_hex(16),
    }, _signing_secret(), algorithm="HS256")


def verify_bearer(db_file: str, authorization: str | None, accepted_roles: set[str]) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required", headers={"WWW-Authenticate": "Bearer"})
    token = authorization[7:].strip()
    if not token or len(token) > 4096:
        raise HTTPException(status_code=401, detail="Invalid bearer token")
    try:
        import jwt
    except ImportError:
        raise HTTPException(status_code=503, detail="PyJWT missing; install requirements_p3.txt")
    try:
        claims = jwt.decode(token, _signing_secret(), algorithms=["HS256"],
                            issuer=ISSUER, audience=AUDIENCE,
                            options={"require": ["exp", "iat", "nbf", "iss", "aud", "sub", "jti", "ver"]})
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid or expired token", headers={"WWW-Authenticate": "Bearer"})
    username = claims.get("sub")
    if not isinstance(username, str) or not USERNAME_RE.fullmatch(username):
        raise HTTPException(status_code=401, detail="Invalid token subject")
    with closing(sqlite3.connect(db_file)) as db:
        row = db.execute("SELECT role, disabled, token_version FROM dashboard_users WHERE username=?",
                         (username,)).fetchone()
    if not row or row[1] or claims.get("ver") != row[2] or claims.get("role") != row[0]:
        raise HTTPException(status_code=401, detail="Session revoked or user disabled")
    if row[0] not in accepted_roles:
        raise HTTPException(status_code=403, detail="Insufficient role")
    return {"username": username, "role": row[0]}
