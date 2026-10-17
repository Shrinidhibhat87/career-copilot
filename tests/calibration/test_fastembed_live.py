"""The real embedding model. Opt-in: downloads ~130 MB into FastEmbed's cache once.

Run with: uv run pytest -m calibration tests/calibration/test_fastembed_live.py
"""

from __future__ import annotations

import pytest

from career_copilot.adapters.embed.fastembed_embedder import FastEmbedEmbedder
from career_copilot.usecases.rank import cosine

pytestmark = pytest.mark.calibration


def test_bge_small_separates_a_forward_deployed_role_from_sales() -> None:
    embedder = FastEmbedEmbedder()
    fde_job, sales_job, exemplar = embedder.embed(
        [
            "Forward Deployed Engineer. Build agentic AI systems in Python on site with "
            "enterprise customers, from prototype to production.",
            "Account Executive. Own a sales quota, run the pipeline and close new "
            "enterprise deals.",
            "Forward Deployed Engineer, working with customers to ship LLM applications.",
        ]
    )
    assert len(fde_job) == 384
    assert cosine(fde_job, exemplar) > cosine(sales_job, exemplar)
