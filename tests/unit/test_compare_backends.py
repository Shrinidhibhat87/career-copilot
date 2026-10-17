"""Comparing two machines' top lists: overlap, rank agreement, score drift."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from career_copilot.__main__ import app
from career_copilot.usecases.compare_backends import compare
from tests.accounts_helper import make_account


@pytest.fixture(autouse=True)
def account(tmp_path: Path) -> None:
    make_account(tmp_path)


def export(model: str, *jobs: tuple[str, int]) -> dict[str, object]:
    return {
        "exportedAt": datetime(2026, 10, 7, tzinfo=UTC).isoformat(),
        "models": [model],
        "jobs": [
            {"url": url, "title": url.upper(), "company": "Co", "score": score, "model": model}
            for url, score in jobs
        ],
    }


VLLM = export("vllm", ("a", 90), ("b", 80), ("c", 70), ("d", 60))
OLLAMA = export("ollama", ("b", 85), ("a", 84), ("c", 70), ("e", 50))


def test_overlap_and_differences() -> None:
    result = compare(VLLM, OLLAMA)
    assert (result.shared, result.only_a, result.only_b) == (3, ("d",), ("e",))
    assert result.jaccard == 3 / 5
    assert result.mean_score_delta == (6 + 5 + 0) / 3


def test_rank_correlation_is_one_for_the_same_order_and_drops_on_a_swap() -> None:
    assert compare(VLLM, VLLM).rank_correlation == 1.0
    swapped = compare(VLLM, OLLAMA).rank_correlation
    assert swapped is not None and swapped < 1.0


def test_too_little_overlap_has_no_correlation() -> None:
    result = compare(export("x", ("a", 1)), export("y", ("b", 1)))
    assert result.rank_correlation is None and result.mean_score_delta is None
    assert result.jaccard == 0.0


def test_cli_prints_the_comparison(tmp_path: Path) -> None:
    a, b = tmp_path / "vllm.json", tmp_path / "ollama.json"
    a.write_text(json.dumps(VLLM))
    b.write_text(json.dumps(OLLAMA))
    out = CliRunner().invoke(app, ["compare", str(a), str(b)]).output
    assert "shared 3, jaccard 0.60" in out
    assert "only in vllm.json: Co — D" in out


def test_export_top_on_an_empty_store(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(f"db_path: {tmp_path / 'jobs.db'}\n")
    out = tmp_path / "top.json"
    result = CliRunner().invoke(app, ["export-top", "--out", str(out), "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text())["jobs"] == []
