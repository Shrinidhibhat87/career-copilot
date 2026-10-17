"""Serving the built frontend from the API process.

One origin means no CORS in the packaged path and no second port for a user to know
about. The build is a Next static export, so this is a directory of files and nothing
runs node on the user's machine.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.types import Scope

BUILD_DIR = Path(__file__).resolve().parents[3] / "frontend" / "out"
BUILD_HINT = "run: cd src/frontend && npm install && npm run build"


class ExportFiles(StaticFiles):
    """`StaticFiles(html=True)` serves `index.html` for a directory but does not map `/resume`
    to `resume.html`, which is how Next exports a route. Try the `.html` file on a miss."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        # A miss is either a raised 404 or, with `html=True`, the export's 404 page.
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            response = None
        missed = response is None or response.status_code == 404
        if missed and path and "." not in path.rsplit("/", 1)[-1]:
            try:
                return await super().get_response(f"{path}.html", scope)
            except HTTPException:
                pass
        if response is None:
            raise HTTPException(status_code=404)
        return response


def mount_ui(app: FastAPI, build_dir: Path = BUILD_DIR) -> bool:
    """Mount the export at `/`, or report that it is not built.

    Returning False rather than raising keeps the API usable: a missing UI build is a
    reason to print an instruction, not a reason for the backend to refuse to start.
    """
    if not (build_dir / "index.html").is_file():
        return False
    app.mount("/", ExportFiles(directory=build_dir, html=True), name="ui")
    return True
