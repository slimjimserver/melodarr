"""Room bootstrap capabilities; never included in ordinary Room snapshots."""

import base64
import hmac
import secrets
from hashlib import sha256

if __package__ == "backend.services":
    from ..config import load_session_secret
else:
    from config import load_session_secret


def _token(room_id, nonce):
    # A random 256-bit nonce yields a separate cryptographically random invite.
    # The persistent server key lets the host share it again without storing the
    # bearer secret. A database-only leak cannot reconstruct it from the nonce.
    digest = hmac.digest(
        load_session_secret().encode(),
        f"melodarr-room-invite-v1:{room_id}:{nonce}".encode(),
        "sha256",
    )
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def create(room_id):
    nonce = secrets.token_hex(32)
    token = _token(room_id, nonce)
    return nonce, sha256(token.encode()).hexdigest()


def share(connection, room):
    """Initialize legacy invitations once, retaining their code and UUID."""
    if not room["invite_nonce"]:
        nonce, verifier = create(room["id"])
        connection.execute(
            "UPDATE rooms SET invite_nonce=?,invite_hash=? WHERE id=?",
            (nonce, verifier, room["id"]),
        )
    else:
        nonce, verifier = room["invite_nonce"], room["invite_hash"]
    token = _token(room["id"], nonce)
    if not valid(verifier, token):
        return None
    return token


def valid(verifier, token):
    return (
        isinstance(token, str)
        and len(token) == 43
        and bool(verifier)
        and hmac.compare_digest(verifier, sha256(token.encode()).hexdigest())
    )
