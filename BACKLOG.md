# Backlog

Open work only. A finished item is deleted, never marked done; shipped work lives in `docs/superpowers/specs/`.

## Backend

1. **Match prompt sends the full resume on every call.** `agents/match.py` numbers and sends the whole resume with each job, so cost grows with resume length. Trim or cache the shared part without breaking line-ID evidence citations. Status: open.

## Frontend

1. **CSV bulk-labeling import.** Top matches export to CSV (`career-copilot export-top`); there is no way to import labels back. Add an import path for bulk labels. Status: open.
2. **Saving preferences blocks on a full re-filter.** `PUT /api/preferences` runs `apply_preferences` over every stored job inside the request, so a save takes as long as re-filtering the whole table. Either return at once and re-filter in a background run with progress (the `runs` table has `completed`/`total`), or re-filter only the jobs the edited fields can change. The screen must keep reporting `refiltered` and `reopenedForScoring`. Status: open.
3. **Nothing distinguishes a live posting from a closed one.** A kept posting can lead to the board's "no longer available" page before `inactive_after_days` retires it. Recommendations only warns with `MAY BE CLOSED`. Weigh a cheap liveness re-probe on rows the user is about to open against the crawl budget (probing must stay throttled), and decide what a confirmed-gone posting becomes: retired early, or kept and struck through. Status: open.
