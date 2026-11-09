"""Local cookie sessions; no provider credential is read by the fixture app."""
import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie, CookieError
from pathlib import Path

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

class SessionManager:
    def __init__(self, db_path, state_dir, *, actor_scope="local-owner", cookie_name="at_session"):
        if actor_scope not in {"local-owner", "w07-qualified-cli"} or cookie_name not in {"at_session", "at_session_qualification", "at_session_conversation", "at_session_operations"}:
            raise ValueError("Unsupported local session profile")
        self.actor_scope, self.cookie_name = actor_scope, cookie_name
        self.db_path = str(db_path)
        key_path = Path(state_dir) / "session.key"
        key_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with key_path.open("xb") as f:
                f.write(secrets.token_bytes(32))
        except FileExistsError:
            pass
        self.key = key_path.read_bytes()
        if len(self.key) != 32:
            raise ValueError("Invalid session key; reconcile local state before startup")

    def _csrf(self, token):
        return hmac.new(self.key, ("csrf:" + token).encode(), hashlib.sha256).hexdigest()

    def lookup(self, header):
        try:
            jar = SimpleCookie()
            jar.load(header or "")
            token = jar[self.cookie_name].value
        except (CookieError, KeyError):
            return None
        if len(token) > 256:
            return None
        with sqlite3.connect(self.db_path, timeout=10) as db:
            row = db.execute("SELECT actor_scope,csrf_nonce_digest FROM local_sessions WHERE session_digest=? AND revoked_at IS NULL AND expires_at>?", (digest(token), datetime.now(timezone.utc).isoformat())).fetchone()
        if not row or row[0] != self.actor_scope or not hmac.compare_digest(row[1], digest(self._csrf(token))):
            return None
        return {"actor": row[0], "csrf_token": self._csrf(token)}

    def issue(self):
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        csrf = self._csrf(token)
        with sqlite3.connect(self.db_path, timeout=10) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("INSERT INTO local_sessions(session_digest,actor_scope,csrf_nonce_digest,created_at,expires_at) VALUES(?,?,?,?,?)", (digest(token), self.actor_scope, digest(csrf), now.isoformat(), (now+timedelta(hours=8)).isoformat()))
        return token, {"actor": self.actor_scope, "csrf_token": csrf}
