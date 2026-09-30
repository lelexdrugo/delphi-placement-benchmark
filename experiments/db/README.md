# experiments/db — Phase-A seed dump / restore wrappers

Operator-side scripts that snapshot the decision-maker Postgres into
a versioned SQL dump and restore it back, so every Phase-B baseline
run sees byte-identical history. Implements the Phase-A freezing
protocol from `experiments/DESIGN.md` §7.

## Files

| File | Purpose |
|---|---|
| `.env.example` | Template for `.env`. Real values are gitignored. |
| `.gitignore` | Ignores `.env`. |
| `port-forward.ps1` | Opens `kubectl port-forward svc/postgres` on on-prem. Keep open while dump/restore runs. |
| `dump-seed.sh` | Bash. Writes `experiments/runs/phase-a/seed-<ts>.sql` + `.sha256`. Uses `pg_dump --clean --if-exists` so the dump can be restored over a non-empty DB (iter-4c.0). |
| `restore-seed.sh` | Bash. Restores a seed file into Postgres; requires `-y` acknowledgement. |
| `truncate-pre-bootstrap.sh` | Bash. Idempotent. Empties the four DELPHI data tables (`decision_requests`, `outbox`, `cluster_snapshots`, `job_metrics`) before a Phase-A bootstrap so the seed is generated against a clean history surface. Prints row counts before and after; exits non-zero if any target table still has rows. `--count-only` shows current counts without truncating. Preserves `schema_migrations` deliberately. Added in iter-4c.0. |

## One-time setup

```powershell
cp experiments\db\.env.example experiments\db\.env
# edit experiments\db\.env: set DB_PASSWORD to your database's password.
```

Verify the .env file is gitignored:

```powershell
git check-ignore experiments\db\.env
# Expected output: experiments/db/.env
```

## Typical workflow

In **one** terminal, open the port-forward (keep it running):

```powershell
.\experiments\db\port-forward.ps1
```

In a **second** terminal (WSL bash recommended for portable
`pg_dump`/`psql` and `sha256sum`):

```bash
# Optional: defensive snapshot of the current state before iter-4c.
bash experiments/db/dump-seed.sh
# -> experiments/runs/phase-a/seed-<UTC timestamp>.sql (+ .sha256)
# Rename to pre-bootstrap-<ts>.sql if this is a defensive snapshot,
# not the actual seed.

# Optional: clean history surface before a fresh Phase-A bootstrap.
# Inspect counts first:
bash experiments/db/truncate-pre-bootstrap.sh --count-only
# Then truncate:
bash experiments/db/truncate-pre-bootstrap.sh
# -> 4 tables empty, schema_migrations preserved.

# After running a Phase-A bootstrap suite, snapshot the resulting DB:
bash experiments/db/dump-seed.sh
# -> experiments/runs/phase-a/seed-<UTC timestamp>.sql (+ .sha256)
```

Before each Phase-B baseline run, restore the snapshot so every
baseline starts from byte-identical history:

```bash
bash experiments/db/restore-seed.sh -y experiments/runs/phase-a/seed-<chosen-timestamp>.sql
```

Since iter-4c.0, `dump-seed.sh` uses `pg_dump --clean --if-exists`,
so the dump can be restored against a DB that still contains DELPHI
rows from a previous run. Without those flags, restore failed at the
first primary-key collision.

The orchestrator's run manifest (`experiments/runs/<run-id>/manifest.json`)
records which seed SHA was used, so reviewers can trace any result
back to the exact starting state.

## Why operator-driven, not automated

`pg_dump`/`psql` invocations are operator-only because:

1. They touch a live multi-tenant database; a command guard can't validate
   destructiveness at the SQL level (a `psql` invocation can DROP
   TABLE without showing it in argv).
2. Credentials live in `experiments/db/.env`, which an automated
   session would have to read to invoke pg_dump — that increases the
   blast radius if the session ever logged the file.
3. The Phase-A protocol expects the operator to choose *when* to
   snapshot (after bootstrap completes, before Phase-B starts), which
   is a coordination decision, not an execution detail.

The agent generates and verifies these scripts; it does not run them
against the live DB. To run iter-3 (which depends on a working
seed/restore cycle), the operator drives this flow once and notes the
seed SHA in the iter-3 worklog.

## Cleanup / safety belts

- `restore-seed.sh` refuses to run without `-y`.
- It validates `.sha256` if present (mismatch -> exit 1).
- Both scripts read credentials only from `.env`; they never accept
  passwords on the command line.
- `port-forward.ps1` does not write to the cluster — it's a read+tunnel.

## Troubleshooting

- *`error: jobmetricsdb does not exist`*: the cluster Postgres has
  not been initialised yet. This is unexpected for this lab — confirm
  with `kubectl --context on-prem exec -n karmada-system deploy/postgres -- psql -l`.
- *`error: password authentication failed`*: `DB_PASSWORD` in `.env`
  is wrong; use the password of your Postgres deployment.
- *`error: pg_dump: command not found`*: install Postgres client tools
  in WSL (`sudo apt install -y postgresql-client`).
