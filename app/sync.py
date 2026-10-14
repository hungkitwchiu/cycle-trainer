"""Uploading finished rides to Garmin Connect.

Garmin has no public upload API for individuals, so this uses the community
`garminconnect` library, which signs in like the Garmin Connect mobile app.
Tokens are cached in the private folder; your password is never stored.
Strava is not called directly: link Garmin Connect to Strava (in Strava's settings)
and every ride uploaded here is forwarded automatically.
"""
from __future__ import annotations

from pathlib import Path

from . import config

# =========================================================================== Garmin
class GarminSync:
    def __init__(self):
        self._client = None
        self._pending = None  # client waiting for an MFA code

    @property
    def tokenstore(self) -> str:
        return str(config.GARMIN_TOKEN_DIR)

    def status(self) -> dict:
        email = config.load_secrets()["garmin"].get("email", "")
        has_tokens = config.GARMIN_TOKEN_DIR.exists() and any(config.GARMIN_TOKEN_DIR.iterdir()) \
            if config.GARMIN_TOKEN_DIR.is_dir() else config.GARMIN_TOKEN_DIR.exists()
        return {"connected": bool(has_tokens), "email": email, "awaiting_mfa": self._pending is not None}

    def login(self, email: str, password: str) -> dict:
        from garminconnect import Garmin

        config.GARMIN_TOKEN_DIR.parent.mkdir(parents=True, exist_ok=True)
        g = Garmin(email=email, password=password, return_on_mfa=True)
        status, _ = g.login()
        if status == "needs_mfa":
            self._pending = (g, email)
            return {"mfa_required": True}
        self._finish(g, email)
        return {"ok": True}

    def submit_mfa(self, code: str) -> dict:
        if not self._pending:
            raise RuntimeError("No Garmin login is waiting for a code")
        g, email = self._pending
        g.resume_login(None, code.strip())
        self._pending = None
        self._finish(g, email)
        return {"ok": True}

    def _finish(self, g, email: str) -> None:
        g.client.dump(self.tokenstore)
        sec = config.load_secrets()
        sec["garmin"]["email"] = email
        config.save_secrets(sec)
        self._client = g

    def logout(self) -> None:
        import shutil

        self._client = None
        self._pending = None
        p = config.GARMIN_TOKEN_DIR
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink()

    def _get_client(self):
        if self._client is None:
            from garminconnect import Garmin

            if not config.GARMIN_TOKEN_DIR.exists():
                raise RuntimeError("Garmin Connect is not connected (Settings > Garmin)")
            g = Garmin()
            g.login(tokenstore=self.tokenstore)
            self._client = g
        return self._client

    def upload(self, fit_path: Path) -> dict:
        try:
            resp = self._get_client().upload_activity(str(fit_path))
        except Exception as e:
            msg = str(e)
            if "409" in msg or "duplicate" in msg.lower():
                return {"ok": True, "duplicate": True, "url": "https://connect.garmin.com/modern/activities"}
            self._client = None
            raise RuntimeError(f"Garmin upload failed: {msg[:300]}") from e
        url = "https://connect.garmin.com/modern/activities"
        try:
            data = resp.json() if hasattr(resp, "json") else resp
            succ = (data or {}).get("detailedImportResult", {}).get("successes") or []
            if succ and succ[0].get("internalId"):
                url = f"https://connect.garmin.com/modern/activity/{succ[0]['internalId']}"
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "url": url}


garmin = GarminSync()
