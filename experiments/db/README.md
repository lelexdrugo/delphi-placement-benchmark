# experiments/db — seed-history dump / restore wrappers

A campaign has two phases. **Phase A** (seed-history bootstrap) runs
`experiments/specs/bootstrap.yaml` once to populate the decision-maker
history. **Phase B** (measured runs) replays a scenario specification
under each policy. These operator-side scripts snapshot the
decision-maker Postgres after Phase A into a versioned SQL dump and
restore it before every Phase-B run, so every baseline run sees
byte-identical history.

## Files

| File | Purpose |
|---|---|
| `.env.example` | Template for `.env`. Real values are gitignored. |
| `.gitignore` | Ignores `.env`. |
| `port-forward.ps1` | Opens `kubectl port-forward svc/postgres` on on-prem. Keep open while dump/restore runs. `port-forward.sh` is the Bash equivalent. |
| `dump-seed.sh` | Bash. Writes `experiments/runs/phase-a/seed-<ts>.sql` + `.sha256`. Uses `pg_dump --clean --if-exists` so the dump can be restored over a non-empty DB. |
| `restore-seed.sh` | Bash. Restores a seed file into Postgres; requires `-y` acknowledgement. |
| `truncate-pre-bootstrap.sh` | Bash. Idempotent. Empties the four DELPHI data tables (`decision_requests`, `outbox`, `cluster_snapshots`, `job_metrics`) before a Phase-A bootstrap so the seed is generated against a clean history surface. Prints row counts before and after; exits non-zero if any target table still has rows. `--count-only` shows current counts without truncating. Preserves `schema_migrations` deliberately. |

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
# Optional: defensive snapshot of the current state before a bootstrap.
bash experiments/db/dump-seed.sh experiments/runs/phase-a/pre-bootstrap-<ts>.sql

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

`dump-seed.sh` uses `pg_dump --clean --if-exists`, so the dump can be
restored against a DB that still contains DELPHI rows from a previous
run. Without those flags, restore fails at the first primary-key
collision.

The run manifest (`experiments/runs/<run-id>/manifest.json`) has a
`phase_a_seed_sha256` field for the SHA of the restored seed. The
orchestrator does not fill it; copy the value from the seed's `.sha256`
sidecar so any result can be traced back to its starting state.

## Why these scripts are run by hand

1. `psql` can drop tables without that showing in its argv (a restore
   file is a sequence of `DROP TABLE IF EXISTS ... CASCADE` and
   `CREATE TABLE` statements), so `restore-seed.sh` requires an explicit
   `-y` before it overwrites the database.
2. Credentials come only from `experiments/db/.env`; the scripts never
   accept passwords on the command line.
3. When to snapshot (after the bootstrap completes, before Phase B
   starts) is a protocol decision, not an execution detail.

## Cleanup / safety belts

- `restore-seed.sh` refuses to run without `-y`.
- It validates `.sha256` if present (mismatch -> exit 1).
- The scripts read credentials only from `.env`; they never accept
  passwords on the command line.
- `port-forward.ps1` does not write to the cluster — it's a read+tunnel.

## Troubleshooting

- *`error: jobmetricsdb does not exist`*: the cluster Postgres has
  not been initialised yet. Confirm with
  `kubectl --context on-prem exec -n karmada-system deploy/postgres -- psql -l`.
- *`error: password authentication failed`*: `DB_PASSWORD` in `.env`
  is wrong; use the password of your Postgres deployment.
- *`error: pg_dump: command not found`*: install Postgres client tools
  in WSL (`sudo apt install -y postgresql-client`).
