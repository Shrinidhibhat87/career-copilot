"""Model-backend health over HTTP, so the settings screen can show what the CLI shows."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter

from career_copilot.adapters.llm.backends import Prober, probe
from career_copilot.config import Config
from career_copilot.usecases.doctor import diagnose

router_prefix = "/api/health"


def router(get_config: Callable[[], Config], prober: Prober = probe) -> APIRouter:
    api = APIRouter(prefix=router_prefix, tags=["health"])

    @api.get("/models")
    def models() -> dict[str, object]:
        config = get_config()
        report = diagnose(config, prober)
        return {
            "ok": report.ok,
            "profile": {"name": report.profile.name, "source": report.profile.source},
            "suggest": report.suggest,
            "roles": [
                {
                    "role": status.role,
                    "kind": status.kind,
                    "model": status.model,
                    "base_url": status.base_url,
                    "reachable": status.reachable,
                    "model_present": status.model_present,
                    "tools": status.tools,
                    "detail": status.detail,
                    # Whether a run on this role costs money; the settings page shows the
                    # spending approval only when one does.
                    "local": config.role(status.role).is_local,  # type: ignore[arg-type]
                }
                for status in report.statuses
            ],
        }

    return api
