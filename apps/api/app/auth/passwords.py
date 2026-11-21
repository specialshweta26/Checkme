"""Password hashing and rules (docs/AUTH.md). scrypt from the standard library: salted, memory-hard, no extra package."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
import secrets

N, R, P = 2**15, 8, 1
MIN_LENGTH = 10
MAX_LENGTH = 200
_COMMON = {
    "password", "password1", "password123", "1234567890", "qwertyuiop", "iloveyou12", "letmein123", "welcome123",
    "admin12345", "passw0rd123", "abcdefghij", "qwerty1234", "1q2w3e4r5t", "0987654321", "lumio12345",
}  # fmt: skip


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=128 * r * n * 2, dklen=32)


def hash_password_sync(password: str) -> str:
    salt = secrets.token_bytes(16)
    return f"scrypt${N}${R}${P}${_b64(salt)}${_b64(_scrypt(password, salt, N, R, P))}"


def verify_password_sync(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = (stored or "").split("$")
        if algo != "scrypt":
            return False
        got = _scrypt(password, _unb64(salt), int(n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, _unb64(digest))


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(hash_password_sync, password)


async def verify_password(password: str, stored: str | None) -> bool:
    """Constant work whether or not there is a stored hash, so timing doesn't tell which emails have an account."""
    return await asyncio.to_thread(verify_password_sync, password, stored or _DUMMY)


_DUMMY = hash_password_sync(secrets.token_urlsafe(16))


def fingerprint(stored: str) -> str:
    """A short value that changes with the password, so a reset or verify link dies once the password changes."""
    return hashlib.sha256((stored or "").encode()).hexdigest()[:16]


def problem(password: str, email: str = "") -> str:
    """Why ``password`` can't be used ('' when it's fine)."""
    pw = password or ""
    if len(pw) < MIN_LENGTH:
        return f"Use at least {MIN_LENGTH} characters."
    if len(pw) > MAX_LENGTH:
        return f"Use at most {MAX_LENGTH} characters."
    low = pw.lower()
    if low in _COMMON or len(set(low)) < 4:
        return "That password is too easy to guess. Try a longer phrase."
    local = (email or "").split("@")[0].lower()
    if len(local) >= 4 and local in low:
        return "Don't use your email address in the password."
    if re.fullmatch(r"\d+", pw):
        return "Use letters as well as numbers."
    return ""
