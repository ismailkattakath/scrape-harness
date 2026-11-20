# Preflight

`<cli> preflight <command ...>` checks what a run needs **before** the
operator's Touch ID. It reads only local state and the environment.

## Why

A bounded refresh once stopped after the approval, because a paid step's
secret env var was unset. The Touch ID was spent for nothing.

## Declare what a step needs

Add `secrets_env` to the command's entry in `pipeline.toml`. It lists env var
NAMES, never values.

```toml
[[utilities]]
name = "geocode"
network = "paid"
secrets_env = ["EX_API_KEY"]
inputs = []
outputs = []
```

Absent means the command needs none.

## Run it

| Run | Checks |
|---|---|
| `<cli> preflight refresh` | every `[crawl.refresh]` step |
| `<cli> preflight fetch-ads --max-fetches 5` | that one command, its args parsed by the real parser |
| `... --json` | one `preflight.v1` object instead of the table |
| `... --egress` | also the egress country (see below) |

`--json` and `--egress` are preflight's own flags wherever they appear.

## What it checks

| Check | When | Passes when |
|---|---|---|
| `secret_env` | the command declares it | the env var is set and not empty |
| `signers` | a networked command, or `refresh` | `<PREFIX>_APPROVAL_SIGNERS` names a readable file with the signer line |
| `data_root` | a site command, or a refresh with a site step | the DATA root is set |
| `cooldown` | the same | the site has no active cooldown |
| `breaker` | the same | the site has no breaker latch |
| `egress` | only with `--egress` | the egress country is the home country |

The egress check goes through the one configured `[crawl.proxy]` url, as a
run's own check does, or direct when none is set. With several urls it is
skipped: the plan picks one, and the run checks that one at its start.

- A secret is reported by NAME, as `present` or `missing`. Its value and its
  length are never printed. A test checks this with a marker value.
- The signers file is named by its env var, never by its path.
- `--egress` sends ONE request to the trace endpoint, never to the site. It
  is off by default, so a plain preflight sends nothing.

## Exit codes

- `0`: every check passed.
- `2`: at least one failed. `missing` names each one.

## Inside `refresh`

`refresh` runs the secret check itself, first. When a declared secret is
unset, it exits 2 and names it. Nothing is planned, claimed, spent or sent,
so the approval stays live for the next try. `refresh --dry-run` prints the
same warning and goes on, like the breaker-latch warning. `refresh --abandon`
needs no secret.
