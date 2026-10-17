"""Sample roles: postings the user holds up as "roles like this".

They are kept as text, not as a pointer to a crawled job, so the agent can still read
one after its board drops the posting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class SampleRole:
    """One sample role. `source` is a `SOURCE_TYPES` key for a resolved link, or
    `manual` for pasted text, which has no `slug`, `source_job_id` or `url`."""

    id: int
    source: str
    slug: str | None
    source_job_id: str | None
    company: str
    title: str
    text: str
    url: str | None
    added_at: datetime
