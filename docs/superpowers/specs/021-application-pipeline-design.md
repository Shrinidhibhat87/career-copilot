# Application Pipeline — Design

> After a person marks a job Applied, the app follows what happens next on a board with three columns, keeps a dated timeline per application, mirrors the stage to Notion where the job is already logged, and downloads everything as an Excel file.
> Core rule: the app is the source of truth; Notion is a mirror that is never read back, and nothing about the pipeline needs Notion to work.
> Builds on spec 010 (the Applied decision and the Notion log) and spec 015 (accounts).

## Decisions

| Topic | Decision |
|---|---|
| The gap | Applied is a decision on a posting (`job_decisions`) and the Notion row says `Applied` forever. Nothing records an interview, an offer or a rejection, and nothing shows what is still alive among many applications. |
| Source of truth | `jobs.db`. Notion is a mirror, never read back. With no Notion connected the pipeline and the Excel file work in full. |
| Stages | `applied`, `interviewing`, `offer`, `rejected`, `withdrawn`. Marking a job Applied puts it on the board as `applied`. Undoing Applied takes it off the board; its timeline rows are kept, so applying again resumes the history. |
| Board | A new `/applications` page with three columns: Applied, Interviewing, Closed. Closed cards carry a tag: Offer, Rejected or Withdrawn. |
| Closed column | Offers sit on top, green, always shown. Rejected and withdrawn are folded: the column shows `Show N rejected or withdrawn` until the person opens it. With a hundred applications and two offers the board stays readable. |
| Moving a card | Drag between columns (native HTML5, no new library) and a stage menu on every card, which is the keyboard and touch way. Moving into Closed asks for the outcome: Offer, Rejected or Withdrawn. Moving a closed card back out is allowed and appends a stage like any other. |
| Hide | Each card has Hide. A hidden card leaves the board and goes to a Hidden list that can be opened and restored. Nothing is deleted; hidden applications stay in the Excel file. |
| Timeline | Every stage change is appended with its date and optional note, and never edited. The card shows it. |
| Notes | One free-text note per application, up to 2,000 characters, the person's own text. |
| Stale | An Applied or Interviewing card with no change for N days shows `Quiet for 16 days`. N defaults to 14 and is adjustable beside the board. It is a badge only; nothing is pushed to Telegram or email. |
| Notion mirror | Only for a job that already has a Notion row (`applications` table). A stage or note change updates that page's Status and Comments. A job never added to Notion makes no call. A Notion failure never blocks the move: the stage is saved, the card shows the failure in plain words, and the next change on that card retries. Writing needs the same key and write capability as today's Add to Notion. |
| Excel | `GET /api/pipeline/export.xlsx`. Sheet `Applications`: one row per application, hidden and closed included, with role, company, location, source, score, applied on, stage, last change, days quiet, note and job link. Sheet `Summary`: a count per stage. A cell that starts with `=`, `+`, `-` or `@` is written as text, because job titles are third-party text. CSV is a later follow-up. |
| PII | Nothing new. Notes are the person's own text in `jobs.db` like decisions, and travel with the account export. No new secret. |
| Doctor | One line: `applications: N open, M closed`. |

## Shapes

```python
# domain/pipeline.py: pure, no I/O.
STAGES: tuple[str, ...] = ("applied", "interviewing", "offer", "rejected", "withdrawn")
def column_of(stage: str) -> str: ...                       # applied | interviewing | closed
def days_quiet(last_change: datetime, now: datetime) -> int: ...
def is_stale(stage: str, last_change: datetime, now: datetime, stale_days: int) -> bool: ...
def arrange(cards: Sequence[Card], show_closed: bool) -> Board: ...   # offers first in Closed, newest first elsewhere
```

```python
# ports.py: ApplicationLog gains one method; StateStore gains the stage methods.
class ApplicationLog(Protocol):
    def record(self, entry: ApplicationEntry) -> NotionResult: ...
    def update(self, page_id: str, status: str, comments: str) -> None: ...   # raises with a user-facing message
```

```sql
-- Migration 38. No PII beyond the person's own notes.
CREATE TABLE application_stages (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    stage  TEXT NOT NULL,
    note   TEXT,
    at     TEXT NOT NULL
);
CREATE INDEX idx_stages_job ON application_stages (job_id, id DESC);
CREATE TABLE application_cards (
    job_id     INTEGER PRIMARY KEY,
    note       TEXT NOT NULL DEFAULT '',
    hidden     INTEGER NOT NULL DEFAULT 0,
    notion_error TEXT,                 -- last failed mirror, fixed text, cleared on success
    updated_at TEXT NOT NULL
);
CREATE TABLE application_settings (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    stale_days INTEGER NOT NULL
);
-- Backfill: every job whose latest decision is `applied` gets an `applied` stage at that decision's time.
```

```
GET  /api/pipeline                       → {columns: {applied, interviewing, closed: {offers, folded}}, hidden, staleDays}
POST /api/pipeline/{job_id}/stage {stage, note?}   → the card; 422 on an unknown stage, 404 on a job not applied
PUT  /api/pipeline/{job_id}/note {note}  → the card
POST /api/pipeline/{job_id}/hide | /unhide
GET  /api/pipeline/{job_id}/timeline     → [{stage, note, at}]
GET  /api/pipeline/settings | PUT {staleDays}      (1 to 365)
GET  /api/pipeline/export.xlsx           → the workbook
```

## Flow

1. The person marks a job Applied on Recommendations (unchanged). The job appears in the Applied column with its date.
2. They open `/applications`. Three columns, a note and a menu on each card, a stale badge where it applies, and a Download Excel button.
3. They drag a card to Interviewing, or use its menu. A `application_stages` row is appended and the card moves.
4. They drag a card to Closed and choose Offer, Rejected or Withdrawn. Offers go to the top of Closed in green; the others fold behind the toggle.
5. If the job has a Notion row, the new stage and note are written to it after the local save. A failure is stored on the card and shown; the move stands.
6. Hide moves a card to Hidden; Restore brings it back to its column. Undo Applied on Recommendations removes the card from the board.
7. Download Excel returns the current state of every application, one row each.

## Testing

- Pure: `column_of` for every stage; arrangement puts offers first and newest first elsewhere; rejected and withdrawn fold; `days_quiet` and `is_stale` at the threshold and on closed stages.
- Store: migration 38 on a database with applied jobs backfills one `applied` stage each; the timeline is append-only; hide and unhide; undo Applied removes the card and keeps the timeline; settings round-trip and stay within 1 to 365.
- API contract: stage change validates the stage and the job; note over the cap is refused; the board shape; the export opens with openpyxl and has the two sheets, the headers, one row per application including hidden ones, and correct summary counts; a title starting with `=` is stored as text.
- Notion mirror against the existing fake client: a job with a Notion row gets Status and Comments updated; a job without one makes no call; a failure leaves the stage changed, stores a message with no key in it, and a later change clears it.
- Frontend (vitest): three columns render; a menu move updates the card; Closed shows only offers until the toggle is used; hide and restore; the stale badge; the download link.
- By hand, after the board branch (with the person's go-ahead before any live Notion call): mark a job Applied, drag it through the columns, close one as Offer and one as Rejected, hide one, open the Excel file, and see one Notion Status update.

## Non-goals

Two-way Notion sync, reading stages from Notion, Telegram or email stale reminders, interview scheduling or a calendar, CSV export, custom stage names, more than three columns, and a drag library.

## Delivery

One spec, three small branches after it, merged in this order:

1. `docs/applications-spec` (this spec)
2. `feat/application-stages` (migration 38 and backfill, pure module, store, API, Notion mirror, doctor line)
3. `feat/applications-export` (`openpyxl`, the workbook, the download route)
4. `feat/applications-board` (the `/applications` page, drag and menu, Closed folding, Hide, stale badge and setting, timeline, notes, Download button)
