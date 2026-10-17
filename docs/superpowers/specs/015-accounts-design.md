# Accounts — Design

> Each person on a machine signs in with a username and a passcode and gets their own database. Core rule: nothing leaves the machine, and one person is signed in at a time. Two parts, **Accounts** and **Account export**; each reads on its own. Builds on the SQLite store (010-persistence-and-lifecycle) and the key store (014-cloud-backend).

# Part 1 — Accounts
## Decisions

| Topic | Decision |
|---|---|
| Account | A folder `data/users/<name>/` with `jobs.db`, `logs/` and `auth.json` (`{salt, hash}`). The accounts are the folders; there is no registry. |
| Name | Matches `^[a-z0-9_-]{1,32}$`. This also keeps a name from reaching outside `data/users/`. |
| Passcode | At least 6 characters. `hashlib.scrypt` with a random salt, checked with `hmac.compare_digest`. No lockout, no recovery: a forgotten passcode means deleting the folder. |
| Login page | One page, one form. With no accounts it opens in create mode; otherwise a "New account" link flips the same form between sign in and create. |
| First account | If no account exists and `data/jobs.db` exists, creating the first account moves that file (with `-wal`, `-shm`, `logs/`) into its folder. Later accounts start empty. |
| Session | One signed-in person per server process, in memory. A random token travels in an `HttpOnly`, `SameSite=Strict` cookie. A server restart signs everyone out. |
| Gate | Middleware answers 401 to every `/api/*` request without the right cookie, except `/api/auth/*`. The static pages stay public; they hold no data. |
| Sign out | Clears the session. Refused with 409 while a run is active: the run worker is one thread for the whole process. |
| Config per person | `Config.db_path` and `scoring.digest_path` point into the signed-in folder. One function builds that config. Routers already take a `get_config` callable, so they do not change. |
| Start-up work | `reclaim_dead_run`, `reclaim_stale_syncs` and `AppSession.start()` move from server start to sign in. `AppSession.end()` runs at sign out and at shutdown. |
| CLI | `career-copilot --user NAME <command>`. With exactly one account it is the default; with several it is required and the error lists them. No passcode: shell access already means file access. |
| Machine-level | `config.yaml`, the company list, and the OpenRouter key are shared by all accounts. Saved model settings live in the database, so each account has its own. |
| Limit | The passcode guards the app, not the file. Someone with disk access can open a `.db`. The server still binds `127.0.0.1`. |

## Shapes

`adapters/accounts.py`: `list_accounts(root)`, `create(root, name, passcode)` (raises `AccountError`), `verify(root, name, passcode)`.

| Endpoint | Body / answer |
|---|---|
| `GET /api/auth/status` | `{accounts: [name], user: name or null, adoptsExisting: bool}` |
| `POST /api/auth/register` | `{name, passcode}`; creates and signs in; 409 name taken, 422 invalid |
| `POST /api/auth/login` | `{name, passcode}`; sets the cookie; 401 for a wrong pair, same answer for an unknown name |
| `POST /api/auth/logout` | clears the cookie; 409 while a run is active |

## Flow

```
any /api/* except /api/auth/*:  cookie != session token -> 401, the browser goes to /login
register:  invalid -> 422;  folder exists -> 409
           first = no accounts;  create folder + auth.json
           first and data/jobs.db exists -> move it in;  sign in
login:     verify fails or no such account -> 401
           session = (name, new token); start-up work on that db; set cookie
logout:    run active -> 409;  AppSession.end(); clear session and cookie
cli:       --user given -> that folder;  one account -> it;  none or several -> error with the list
```

## Testing

| Layer | What it proves |
|---|---|
| CI unit `test_accounts.py` | Names with `..`, `/`, capitals or 33 characters are refused. Right passcode verifies, wrong does not. `data/jobs.db` moves for the first account only. |
| CI contract `test_auth_api.py` | `/api/*` is 401 without the cookie, `/api/auth/*` is not. A CV saved by one account is a 404 for the other. Wrong passcode and unknown name give the same 401. Logout is 409 during a run. |
| CI unit (CLI) | `--user` resolves the folder; one account is the default; several without `--user` fail with the list. |
| CI frontend (vitest) | Create mode shows with no accounts, the link flips the form, a 401 sends the browser to `/login`. |
| Manual | Two accounts on the real machine: the first keeps the old data, the second starts empty. Correctness is judged by the second never seeing the first's data. |

# Part 2 — Account export

Needs Part 1; changes nothing in it.
## Decisions

| Topic | Decision |
|---|---|
| What it is | A copy of the account's `jobs.db` the person downloads and keeps where they choose. The app uploads it nowhere. |
| Contents | The CV, preferences, decisions and run history. Not the OpenRouter key and not the logs; the key is entered again on the new machine. |
| Whole copy | `sqlite3.Connection.backup` (standard library), so an export taken while the app is open is complete. |
| Import | Only while creating an account: create mode on the login page offers "Import from file". The account gets the passcode typed there, not the old one. |
| Delete | `DELETE /api/auth/account` with the passcode removes the signed-in account's whole folder and signs out. Refused with 409 during a run and 401 for a wrong passcode. The Settings card shows it in red and asks for the passcode first. |
| Version check | The file's highest row in `schema_migrations` must not exceed the app's last migration, else it is refused. An older file opens through `migrate()`. |

## Shapes

| Endpoint | Body / answer |
|---|---|
| `GET /api/account/export` | `career-copilot-<name>.db` |
| `POST /api/auth/import` | Multipart `name`, `passcode`, `file`; creates and signs in; 422 for a file that is not a usable career-copilot database, 409 name taken |

## Flow

```
export:    backup(jobs.db) -> temp file; stream it; delete the temp file
import:    open the upload read-only; no schema_migrations or version > MIGRATIONS[-1] -> 422, no folder
           else create the account as in Part 1, place the file as jobs.db, migrate(), sign in
```

## Testing

| Layer | What it proves |
|---|---|
| CI unit | An export taken during a write opens and passes `PRAGMA integrity_check`. |
| CI contract | Export from account A, import into new account B: the CV and decisions match. A newer version or a non-career-copilot file gives 422 and leaves no folder. No key is in the file. |
| CI frontend (vitest) | Import shows in create mode only; a refused file shows its message. |
| Manual | Export here, import on a second machine; the CV and dismissed jobs carried over and the key must be re-entered. |

## Non-goals

Encrypting the database or the export, passcode recovery or lockout, several people signed in at once, syncing or merging accounts, scheduled backups, a remote or cloud account, per-account OpenRouter keys.
