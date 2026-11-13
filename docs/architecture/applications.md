# Application pipeline

What happens after a job is marked Applied. Design: spec 021.

Back to the [architecture index](../architecture.md).

## The board

`/applications` has three columns: Applied, Interviewing and Closed. A card moves by drag
and drop (native HTML5) or by its "Move to…" menu, which is the keyboard and touch way.
Moving into Closed asks for the outcome.

| Stage | Column | Shown as |
|---|---|---|
| `applied` | Applied | |
| `interviewing` | Interviewing | |
| `offer` | Closed | on top, green |
| `rejected`, `withdrawn` | Closed | folded behind "Show N rejected or withdrawn" |

Marking a job Applied on Recommendations puts it on the board; undoing Applied takes it
off and keeps its history. Each card has a dated timeline (append-only, one row per
change), one note of up to 2,000 characters, and Hide. A hidden card moves to a Hidden
list and can be restored; nothing is deleted. An open card with no change for N days
shows "Quiet for N days" (default 14, adjustable from 1 to 365). It is a badge only;
nothing is pushed.

## Source of truth

The app is: stages, notes and hides live in the account's `jobs.db`
(`application_stages`, `application_cards`, `application_settings`), and the pure layout
rules are in `domain/pipeline.py`. Notion is a mirror and is never read back, so the board
and the Excel file work with no Notion at all.

For a job that already has a Notion row, a stage or note change updates that page's
Status and Comments after the local save. A Notion failure never blocks the move: the
card shows the reason and the next change on it retries.

## Excel

`GET /api/pipeline/export.xlsx` (the Download Excel button) returns every application,
hidden and closed included. Sheet `Applications` has one row each; sheet `Summary` counts
per stage. A cell starting with `=`, `+`, `-` or `@` is written as text, because job
titles are third-party text. CSV is not built.

## API

`GET /api/pipeline` (board), `POST /api/pipeline/{job_id}/stage`, `PUT …/note`,
`POST …/hide` and `…/unhide`, `GET …/timeline`, `GET`/`PUT /api/pipeline/settings`.
Code: `adapters/api/pipeline.py`, `adapters/export/applications_xlsx.py`.
