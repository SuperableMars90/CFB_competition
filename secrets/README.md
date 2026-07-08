# secrets/

Everything in this directory (except this file and the `*.example.toml`/
`.example` templates) is real, gitignored, and never committed. See
`../docs/INSTALL.md` for the full setup walkthrough. Quick reference:

| Real file | Copy from | Used by |
|---|---|---|
| `aiven.toml` | `aiven.example.toml` | `db/connection.py` (every CLI script under `scripts/`) |
| `wordpress.toml` | `wordpress.example.toml` | `scripts/wordpress_client.py` — only needed if pushing pages to WordPress |
| `cfbd_api_key` | `cfbd_api_key.example` | `scripts/cfbd_client.py` |
| `ca.pem` | *(not a template — see below)* | referenced by `ssl_ca_path` in `aiven.toml` |

**`ca.pem` isn't something you fill in a template for** — it's the real
CA certificate your database provider gives you for verifying the TLS
connection (a public certificate, not a secret; Aiven's console has a
direct download for it). Get the real file from your provider and save
it here.

**`local_mysql.toml` is a leftover, not a supported option.** It looks
like a non-TLS alternative from its name, but nothing in this codebase
currently reads it — both `lib/db.py` and `db/connection.py` hardcode
`ssl_verify_cert=True` with no way to disable it today. Real non-TLS
support would need a small code change, not a config one. Kept here as
a known gap (tracked in the post-release roadmap, kept outside this
repo alongside `PROGRESS.md`) rather than deleted, since it's harmless
either way — but don't fill it in expecting it to do anything yet.
