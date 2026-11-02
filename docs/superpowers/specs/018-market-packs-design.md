# Market Packs — Design

> Makes the market a per-account setting: which countries a person searches in, which free national sources that turns on, and which location and language rules apply to each job. Germany is the only ready pack; adding a country becomes a data addition.
> Core rule: a job is judged by the rules of the country it is in, and until the person confirms a market, behaviour is exactly what it is today (Germany).
> Builds on spec 005 (preferences), spec 015 (accounts) and spec 017 (source health).

## Decisions

| Topic | Decision |
|---|---|
| The gap | Germany is hardcoded: one national board switched on in `config.yaml`, German-only language patterns, a relocation pattern naming German cities, and "outside Germany" reasons. There is no country field anywhere. |
| Unit | A market is a country (ISO 3166 alpha-2 code). A pack is the code-defined bundle for it. Packs live in code, not in the database or `config.yaml`. |
| Per account | Each account has its own enabled markets, one or more (for example `DE`, `ZA`, `IN`). Stored in the account's `jobs.db`. |
| A pack holds | Code, name, local language, country words, search sources, default radius, and a status: `ready` or `soon`. |
| Ready now | `DE` only (national board `jobsuche`). |
| Coming soon | Other countries are listed and cannot be switched on. No source is named for a country until it is verified (research belongs to the free-sources work). |
| Default | Never applied silently. `/preferences` shows a proposal and the person confirms. From `allowed_cities` through a small city-to-country table first, from the CV second (a CV shows where someone worked, not where they want to work). Until confirmed, the account behaves as `DE`. |
| A job's market | The country in the job's own location text. If it is an enabled market, that pack's rules apply. If it is none, the job is filtered out as `location outside your markets`. |
| No country in the text | The city table decides. A city it does not know keeps the job, as today. |
| Remote | Fully remote jobs are allowed by default in every market, with no market check. The person switches them off with `allow_remote` on `/preferences`. |
| Language rule | Patterns are keyed by language. The `DE` pack uses the existing German patterns, moved and unchanged. A pack whose language has no patterns does no language filtering and shows as limited. |
| Search sources | Which searched sources run comes from the enabled packs. `jobsuche.enabled` in `config.yaml` stays as a global off switch. |
| Changing markets | Saving re-filters stored jobs, the way saving preferences does. |
| Reason text | New reason `location outside your markets`. The old `location outside Germany` and `remote role with non-German location` are still read, so no stored row is rewritten. |

## Shapes

```python
# domain/markets.py: pure, no I/O.
@dataclass(frozen=True, slots=True)
class Market:
    code: str                      # "DE"
    name: str                      # "Germany"
    language: str                  # "german": key into the language patterns
    country_words: tuple[str, ...] # ("germany", "deutschland", "de")
    sources: tuple[str, ...]       # ("jobsuche",): searched sources this pack turns on
    radius_km: int
    ready: bool

MARKETS: dict[str, Market]
def enabled(codes: Sequence[str]) -> tuple[Market, ...]: ...     # ready packs only; empty means ("DE",)
def country_of(location: str, markets: Sequence[Market]) -> str | None: ...
def propose(cities: Sequence[str], cv_text: str | None) -> tuple[str, ...]: ...
```

```sql
-- Migration 34, versioned rows like model_settings: the newest row is in force.
CREATE TABLE market_settings (
    version      INTEGER PRIMARY KEY,
    codes        TEXT NOT NULL,        -- JSON list of codes
    confirmed_at TEXT NOT NULL
);
```

```
GET /api/markets  → {packs: [{code, name, language, ready, limited, sources}],
                     enabled: [code], confirmed: bool, proposal: [code]}
PUT /api/markets  {codes: [code]} → same shape; re-filters stored jobs
career-copilot doctor   # one line: markets: DE (confirmed) | DE (default, not confirmed)
```

## Flow

1. `/preferences` shows a Markets section: a chip per pack, "coming soon" ones disabled, and the proposal with a Confirm button until the person confirms.
2. Saving writes a new `market_settings` row and re-filters stored jobs, as a preferences save does.
3. A run builds its searched targets from the enabled packs' sources. A source is skipped when `jobsuche.enabled` is false or no enabled pack lists it. Health rows (spec 017) are unchanged.
4. The hard filters get the enabled markets. The location check takes the job's country from its text, applies that pack's country words and language patterns, and drops jobs in no enabled market.
5. `doctor` prints the enabled markets and whether they are confirmed.

## Testing

- Pure: `country_of` for each pack's words, city-only text and unknown text; `propose` from cities, then CV, then nothing; `enabled` falls back to `DE`.
- Filters: an account with `DE` only gives the same result as before for every existing filter test, unchanged. With `DE` plus a second ready pack in a test, a job in each country follows its own pack, and a third country is dropped as `location outside your markets`.
- Old reason strings still count in the funnel and the run view.
- Store: migration 34 on an existing database, versions round-trip, newest wins.
- API contract: shape, `soon` packs cannot be enabled (400), saving re-filters.
- Pipeline: no enabled pack that lists `jobsuche` means no searched targets.
- By hand, after the UI branch: on a copy of a real account, `/preferences` proposes Germany from the saved cities, Confirm saves it, and the job counts on `/recommendations` do not change.

## Non-goals

Naming or building sources for other countries, detecting the language of an ad, per-market preferences, per-market scoring, translating a market's job text, and a pack editor in the app.

## Delivery

One spec, five small branches, merged in this order:

1. `docs/market-packs-spec` (this spec)
2. `feat/market-settings` (pack module, migration 34, store, API)
3. `feat/market-sources` (searched targets come from the enabled packs)
4. `feat/market-filters` (location, language and reason text follow the pack)
5. `feat/market-ui` (Markets section on `/preferences`, doctor line)
