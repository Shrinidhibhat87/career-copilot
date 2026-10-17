# Frontend Onboarding — Design

> The first screens of the app: a sign-in page, a Profile page where the CV is uploaded and read, and the header that links the steps. Core rule: the app never asks for something it already has, and the CV text never leaves this machine unredacted. Builds on 001-foundation (hexagonal split, FastAPI as a driving adapter), 004-pii-gateway (the privacy block) and 010-persistence-and-lifecycle (the stored CV).

## Decisions

| Topic | Decision |
|---|---|
| Location | The app is a Next.js static export in `src/frontend`, a sibling of `src/career_copilot`. `pyproject.toml` holds no Node rules. |
| Serving | `career-copilot ui` serves the built `src/frontend/out` and the API on one port via `mount_ui`. In development the page reads `NEXT_PUBLIC_API_URL`, else the local API. CORS allows one origin: `FRONTEND_ORIGIN`, default `http://localhost:3000`. |
| Stack | Next.js App Router, React, strict TypeScript, Tailwind, vitest with Testing Library. Build is `next build --webpack`. No animation, query or state libraries. |
| Sign-in | No real authentication. Email and passphrase are optional and never sent. "Sign in" and "Create account" do the same thing. |
| Landing | After either button the app asks `GET /api/resume`. A CV exists: go to `/recommendations`. 404: go to `/resume`. Any other failure stays on the page with "Could not reach the server. Try again." |
| Waiting | While the check runs both buttons are disabled and the page says "Checking your profile…". |
| Header | `SIGN IN`, `PROFILE`, `PREFERENCES`, `RECOMMENDATIONS` are links on every page, plus `SETTINGS` outside the flow. The current one has `aria-current="page"`. On narrow screens the steps collapse to "STEP n OF 4". |
| Look | Colours, radii and fonts are named CSS variables in `globals.css`. Fonts load through `next/font/google`, so the browser makes no runtime font request. No inline `style` in components. |
| Responsive | Two equal columns from 768px. Below that the brand panel stacks above the form. No horizontal scroll at 320px; usable at 200% zoom. |
| Fixture metrics | The three brand-panel numbers in `app/page.tsx` are fixed sample text, marked as such in a comment. |
| CV storage | An upload is saved in `cv_versions` (migration 9) by the hash of its text. Re-uploading the same text is not a new version. Nothing is held in session memory, so a restart keeps the CV. |
| Formats | PDF and DOCX only, up to 5 MB. The size is checked while streaming, so an oversized file is never fully read. |
| Failures | 415 wrong type, 413 too large, 422 no readable text, 502 anything else. Each shows its message inline; the drop zone stays usable. A failure never clears an already stored CV. |
| Detection | Deterministic, local, no model. Skills come from the saved preferences' `core_skills`, so a chip is a term postings are really matched on. Roles come from a role-family lexicon. A language without a level gets `level: null`, not a guess. Output order is stable. |
| Profile states | Loading shows a skeleton card. The drop zone shows only after a 404. Parsed shows the file card, detected skill chips, `REMOVE`, and the continue button. A failed read shows the error, not the drop zone. |
| Remove | `REMOVE` calls `DELETE /api/resume`. If that fails the CV stays on screen with an error. |
| Privacy text | The two notices and the redacted preview sit in one closed `<details>` named "Privacy details". When `ownerAnchor` is `likely`, a "Check name" tag shows on the summary line. |
| Drop zone copy | "max 5 MB · read by the local API on this machine · redacted before any model call". It never claims the file is not uploaded, because it is sent to the local API. |
| Slow read | Name and PII detection run on every `GET /api/resume`. The result is cached in memory by `(cv sha256, skill terms)`, so a repeat read is instant. A new upload or delete clears the cache. Nothing derived is written to SQLite. |
| Warm start | At server start the PII analyzer is built on its own daemon thread (`warm_analyzer`). It never blocks startup and never raises. |
| Continue | `Review preferences →` goes to `/preferences`. If a company list is connected on this page, its sync starts first; if that fails the user can continue without it. |
| Preferences buttons | "Continue to Recommendations" is the filled button and shows only when `prefs.version > 0`. "Save preferences" is the outlined one. |
| Direct URLs | A refresh on `/resume` or any sub-route works: `ExportFiles` in `static_ui.py` tries `<path>.html` on a miss. |
| Accessibility | One `h1` per page, a labelled file input (not a click-handler div), live-region status, errors tied to the input with `aria-describedby`, focus moves to the file name after a parse, a visible focus ring, reduced-motion respected. |

## Shapes

```ts
// lib/landing.ts
type Landing = "/recommendations" | "/resume";
async function routeAfterSignIn(): Promise<Landing>; // 404 -> "/resume"; other errors throw
```

```text
POST   /api/resume           multipart field `file` -> 200 payload (adds version, sha256, changed)
GET    /api/resume           -> 200 payload | 404
GET    /api/resume/versions  -> [{version, filename, uploadedAt, current}]
DELETE /api/resume           -> 204
```

```ts
type ResumeDetections = {
  filename: string; parsedAt: string;                 // ISO 8601
  summary: { roles: number; skills: number; languages: number }; // counted on the server
  detected: { roles: string[]; skills: string[]; languages: { name: string; level: CefrLevel | null }[] };
  privacy: { enabled: boolean; counts: Record<string, number>;
             redactedPreview: string | null;          // null: redaction off or detector failed
             ownerAnchor: "confirmed" | "likely" | "none"; // a level, never a value
             unavailableReason: string | null };
  version: number; sha256: string; changed: boolean;
};
```

```python
# adapters/api/app.py
_resume_cache: dict[tuple[str, tuple[str, ...]], ParsedResume]  # (cv sha256, skill terms)
def warm_analyzer(redactor: object | None) -> None  # logs and returns on any failure
# adapters/resume/extract.py
def extract(filename: str, data: bytes) -> str  # pypdf / python-docx; raises ResumeExtractionError
```

## Flow

```
sign_in_click:
    disable buttons; show "Checking your profile…"
    try:    landing = routeAfterSignIn()
    except: show "Could not reach the server. Try again."; enable buttons; stop
    router.push(landing)

POST /api/resume:
    if suffix not in {.pdf,.docx} or content-type not allowed: 415
    data = read in chunks; if total > MAX_BYTES: 413
    try:    text = extract(filename, data)
            saved = store.save_cv(filename, text, now)   # same hash -> changed=False
            parsed = analyse_resume(...); _resume_cache.clear()
    except ResumeExtractionError: 422       # empty, encrypted or image-only
    except Exception:             502
    return payload(saved.cv, parsed, changed)

GET /api/resume:
    cv = store.current_cv(); if None: 404
    key = (cv.sha256, skill_terms)
    if key not in _resume_cache: _resume_cache = {key: analyse_resume(cv.text)}
    return payload

profile_mount:
    show skeleton; cv = getResume()
    None -> drop zone; error -> error text; else -> parsed card
    if the user acted first, ignore the late mount answer

lifespan_start: thread(warm_analyzer, redactor)   # never blocks, never raises
```

## Testing

| Layer | What it proves |
|---|---|
| pytest `test_resume_api.py` | Upload returns detections and counts; a stored CV is readable again, survives a restart, and is cleared by `DELETE`; 415, 413, 422, 502 each map correctly; a failed upload leaves the CV intact; the payload carries the privacy block and the anchor level but no derived value; two reads run analysis once and a new upload runs it again. |
| pytest `test_cv_store.py` | Versions are keyed by text hash; the newest is current. |
| pytest unit `test_resume_extract.py`, `test_resume_detect.py` | Extraction of PDF and DOCX, typed error on unreadable files; skills match whole words only (`java` is not in `javascript`); level `null` without a CEFR token. |
| pytest `test_warm_analyzer.py` | A failing redactor is logged, not raised. |
| vitest `sign-in`, `resume`, `privacy-badge`, `preferences` | 404 routes to `/resume`, a CV to `/recommendations`, a network error stays put; skeleton before dropzone; the notices are inside a closed `<details>` and "Check name" shows only for `likely`; button classes and the version-0 rule. |
| CI | `.github/workflows/ci.yml` runs ruff, mypy and pytest only. vitest, lint, typecheck and `npm run build` are run by hand with `npm run test|lint|typecheck|build` in `src/frontend`. |
| Manual | Look at 972x600, 768x1024 and 320x568. Restart the server and sign in: Recommendations opens; Profile shows no empty-box flash. |

Correctness is judged by assertions on status codes, payload fields and call counts, never by timing.

## Non-goals

Real authentication or accounts, LLM-based CV extraction, and changing what the PII gateway redacts.
