# Installing CFB Game on a new machine

This covers getting the code running from a fresh clone through a working
Streamlit app with an empty database — not season setup itself (drafting,
scheduling, etc.), which belongs in its own commissioner-facing checklist.

## Prerequisites

- **Python 3.13+** (developed against 3.13.7)
- **A MySQL-compatible database that supports TLS with a CA certificate.**
  This project was built against [Aiven](https://aiven.io)'s free-tier
  MySQL, which gives you a CA cert to download directly from its console.
  **This is a real, current requirement, not just a recommendation** — both
  the Streamlit app (`lib/db.py`) and every CLI script (`db/connection.py`)
  hardcode `ssl_verify_cert=True` with no way to disable it today. A plain
  local MySQL without TLS configured won't work with either of those two
  — only the one-time schema bootstrap below (`db/init_db.py`) can target
  a non-TLS DB, which isn't useful on its own since nothing else could
  then connect to it. (`secrets/local_mysql.toml` looks like a non-TLS
  option from its name — it's a leftover from before this project
  migrated to Aiven and nothing currently reads it. Adding real non-TLS
  support would be a small code change, not a config one, if you need it.)
- **A CollegeFootballData.com (CFBD) API key.** Free at
  <https://collegefootballdata.com/key> — needed to pull conferences,
  teams, and the game schedule.
- **(Optional) A WordPress site with an Application Password**, if you
  want the commissioner-facing scripts that push rosters/records/recap
  pages to a public site. Not needed to run the player-facing app itself.

## 1. Clone and install dependencies

```bash
git clone <this-repo-url> cfb_game
cd cfb_game
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Create the database

Copy the example config and fill in real values:

```bash
cp config/config.example.json config/config.json
```

Edit `config/config.json` — at minimum the `database` block (host/port/
user/password/database name) needs to point at a MySQL server you can
create a database on. Then bootstrap the schema:

```bash
python db/init_db.py
```

This creates the `cfb_game` database (if it doesn't exist) and every
table in `db/schema.sql`. Safe to re-run — it skips tables that already
exist rather than erroring.

Note: `db/init_db.py` itself doesn't pass any TLS parameters — it's the
one script in this repo that doesn't need `secrets/`. Whether that
matters depends on your provider: if it enforces certificate
verification for every connection, this step may need to run from
somewhere that already trusts its cert (or you may need to add
`ssl_ca` here too). Not verified against a fresh Aiven project as part
of writing this doc — flagging it rather than guessing.

## 3. Set up credentials (three places, on purpose)

This project grew two separate credential-loading mechanisms for two
separate runtime contexts, plus the legacy one from step 2 — all three
need to be filled in, none of them share a file:

| File | Used by | Format |
|---|---|---|
| `config/config.json` | `db/init_db.py` (one-time schema bootstrap); `ingest_draft.py`; fallback for `scripts/cfbd_client.py` | JSON |
| `secrets/*.toml` + `secrets/cfbd_api_key` | Everything under `scripts/` — these run as plain CLI processes (season init, weekly scoring, playoff advancement, WordPress pushes) with no Streamlit context, so they can't read `st.secrets` | TOML / plain text |
| `.streamlit/secrets.toml` | The Streamlit app itself (`streamlit_app.py`, `pages/*`, `lib/db.py`, `lib/auth.py`) | TOML, Streamlit's own secrets format |

**`secrets/` directory** (create each file; none of this is committed —
see `.gitignore`):

- `secrets/aiven.toml` — your TLS MySQL connection details:
  ```toml
  [aiven_mysql]
  host = "..."
  port = 12345
  user = "..."
  password = "..."
  database = "cfb_game"
  ssl_ca_path = "secrets/ca.pem"
  ```
  and `secrets/ca.pem` — the CA certificate your DB provider gives you
  for verifying the TLS connection (Aiven's console has a direct
  download for this; it's a public certificate, not a secret). The
  section name is always `[aiven_mysql]` regardless of your actual
  provider — `db/connection.py` reads that key literally.
- `secrets/cfbd_api_key` — a single line, just the raw API key string,
  no quotes.
- `secrets/wordpress.toml` — only needed if pushing pages to WordPress:
  ```toml
  [wordpress_creds]
  url = "https://yoursite.example.com"
  username = "..."
  password = "..."   # a WordPress Application Password, not your login password
  ```

**`.streamlit/secrets.toml`** — same DB credentials again, in Streamlit's
format, plus one more section for player logins:

```toml
[aiven_mysql]
host = "..."
port = 12345
user = "..."
password = "..."
database = "cfb_game"
ssl_ca_path = "secrets/ca.pem"
ssl_ca = """
-- paste the full contents of your ca.pem here, if using TLS --
"""

[auth]
# one line per player: the *exact-case* username matching that
# player's `players.username` column, mapped to their login password.
# Login is currently case-sensitive (see PROGRESS.md — a known,
# tracked papercut).
alice = "somepassword"
bob = "anotherpassword"
```

## 4. A couple of constants worth reviewing before your first season

`lib/config.py`:
- `CURRENT_SEASON_ID` — hardcoded to whichever season the app should
  currently show. A fresh install should point this at the season row
  you create (see the commissioner checklist for season setup).
- `DRAFT_PICKS_PER_PLAYER` — the single source of truth for how many
  teams each player drafts (25 in the original game's rules). Change
  here if you're adapting the rules for a different group.

## 5. Run it

```bash
streamlit run streamlit_app.py
```

## 6. Run the tests

```bash
PYTHONPATH=. pytest -m "not integration"
```

Tests tagged `integration` hit the live database directly (need
`secrets/aiven.toml` and network access) — included when you omit the
`-m` filter, but skipped above since a fresh install's DB is empty and
these tests expect real season data.

## What's next

This gets you a running app against an empty database. Actually
initializing a season (pulling the schedule from CFBD, running a draft,
generating the PVP matchup schedule, and so on) is a separate,
commissioner-facing workflow — see the commissioner checklist
(`docs/commissioner_checklist.md`) for that, script by script.
