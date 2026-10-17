"""Sign in, sign out and the gate in front of the API (spec 015).

One person is signed in per server process, held in memory: a restart signs everyone out.
The token travels in an HttpOnly cookie. Every `/api/*` route except these needs it.
"""

from __future__ import annotations

import secrets
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from career_copilot.adapters import account_export, accounts
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config

COOKIE = "cc_session"
_PREFIX = "/api/auth"
MAX_IMPORT_BYTES = 500 * 1024 * 1024


@dataclass
class _Session:
    name: str
    token: str


_session: _Session | None = None


def current_user() -> str | None:
    return _session.name if _session else None


def authenticated(request: Request) -> bool:
    sent = request.cookies.get(COOKIE, "")
    return _session is not None and secrets.compare_digest(sent, _session.token)


def install(app: FastAPI) -> None:
    """The gate. Register before CORS so a 401 still carries its headers."""

    @app.middleware("http")
    async def gate(request: Request, call_next: Callable[..., object]) -> Response:
        path = request.url.path
        guarded = path.startswith("/api/") and not path.startswith(f"{_PREFIX}/")
        if guarded and request.method != "OPTIONS" and not authenticated(request):
            return JSONResponse({"detail": "Sign in first."}, status_code=401)
        return await call_next(request)  # type: ignore[no-any-return, misc]


class Passcode(BaseModel):
    passcode: str


class Credentials(BaseModel):
    name: str
    passcode: str


def router(
    base_config: Callable[[], Config],
    *,
    on_login: Callable[[str], None],
    on_logout: Callable[[], None],
    run_active: Callable[[], bool],
) -> APIRouter:
    api = APIRouter(prefix=_PREFIX, tags=["auth"])

    def root_and_legacy() -> tuple[Path, Path]:
        db = base_config().db_path
        return accounts.users_root(db), Path(db)

    def sign_in(name: str, response: Response) -> None:
        global _session
        if _session is not None:
            on_logout()
        _session = _Session(name, secrets.token_urlsafe(32))
        on_login(name)
        response.set_cookie(COOKIE, _session.token, httponly=True, samesite="strict")

    def refuse_while_running() -> None:
        if run_active():
            raise HTTPException(409, "A run is active. Wait for it to finish, then sign out.")

    @api.get("/status")
    def status(request: Request) -> dict[str, object]:
        root, legacy = root_and_legacy()
        names = accounts.list_accounts(root)
        return {
            "accounts": names,
            # Only the holder of the cookie is told who is signed in.
            "user": current_user() if authenticated(request) else None,
            # With no account yet, the first one takes over the data from before accounts.
            "adoptsExisting": not names and accounts.has_legacy_data(legacy),
        }

    @api.post("/register")
    def register(body: Credentials, response: Response) -> dict[str, str]:
        name = body.name.strip().lower()
        root, legacy = root_and_legacy()
        if _session is not None and _session.name != name:
            refuse_while_running()
        try:
            accounts.create(root, name, body.passcode, legacy)
        except accounts.AccountError as error:
            raise HTTPException(409 if error.taken else 422, str(error)) from None
        sign_in(name, response)
        return {"user": name}

    @api.post("/import")
    async def register_import(
        response: Response,
        name: Annotated[str, Form()],
        passcode: Annotated[str, Form()],
        file: Annotated[UploadFile, File()],
    ) -> dict[str, str]:
        """Create an account whose database is an uploaded export. Checked first, so a
        refused file leaves no account behind."""
        name = name.strip().lower()
        root, _ = root_and_legacy()
        if _session is not None and _session.name != name:
            refuse_while_running()
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=root, suffix=".upload", delete=False) as out:
            upload = Path(out.name)
            total = 0
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_IMPORT_BYTES:
                    upload.unlink()
                    raise HTTPException(413, "That file is too large to be an export.")
                out.write(chunk)
        try:
            account_export.check_import(upload)
            # No legacy path: an import is a deliberate choice of data, not an adoption.
            accounts.create(root, name, passcode, root / ".none")
            shutil.move(upload, root / name / "jobs.db")
            SqliteStore(root / name / "jobs.db").close()  # migrate an older export forward
        except account_export.ImportRefused as error:
            raise HTTPException(422, str(error)) from None
        except accounts.AccountError as error:
            raise HTTPException(409 if error.taken else 422, str(error)) from None
        finally:
            upload.unlink(missing_ok=True)
        sign_in(name, response)
        return {"user": name}

    @api.post("/login")
    def login(body: Credentials, response: Response) -> dict[str, str]:
        name = body.name.strip().lower()
        root, _ = root_and_legacy()
        if not accounts.verify(root, name, body.passcode):
            raise HTTPException(401, "That username and passcode do not match.")
        if _session is not None and _session.name != name:
            refuse_while_running()
        sign_in(name, response)
        return {"user": name}

    @api.delete("/account", status_code=204)
    def delete_account(request: Request, body: Passcode) -> Response:
        """Delete the signed-in account and its folder. The passcode is asked again: a
        left-open browser must not be enough to erase someone's data."""
        global _session
        # Under /api/auth/, so the gate lets it through; it checks the session itself.
        if _session is None or not authenticated(request):
            raise HTTPException(401, "Sign in first.")
        refuse_while_running()
        root, _ = root_and_legacy()
        name = _session.name
        if not accounts.verify(root, name, body.passcode):
            raise HTTPException(401, "That passcode does not match.")
        on_logout()
        _session = None
        shutil.rmtree(root / name)
        out = Response(status_code=204)
        out.delete_cookie(COOKIE)
        return out

    @api.post("/logout", status_code=204)
    def logout(response: Response) -> Response:
        global _session
        if _session is not None:
            refuse_while_running()
            on_logout()
            _session = None
        out = Response(status_code=204)
        out.delete_cookie(COOKIE)
        return out

    return api
