# scrape-harness

[![ci](https://github.com/ismailkattakath/scrape-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/ismailkattakath/scrape-harness/actions/workflows/ci.yml)
[![licence: MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)
[![Nix flake](https://img.shields.io/badge/Nix-flake-5277C3.svg?logo=nixos&logoColor=white)](flake.nix)

A **site-agnostic harness for careful, approval-gated scraping pipelines.**
The harness drives; a model or a person only assists. Every networked run is
bounded, signed off by the operator, polite, and stops at the first sign that
the site does not want it.

The harness holds no site. A **consumer** project supplies the site-specific
parts (a source plug-in, its record schemas and a `pipeline.toml`) and gets the
CLI, the safety checks and the tooling from this flake.

## What it gives a consumer

| Piece | What it does |
|---|---|
| **Pipeline contract** | `pipeline.toml` (`pipeline.v1`) declares the stages, their inputs and outputs, their network class and the consumer's world-visible names. The CLI, the run card, the store and the hooks are all derived from it. |
| **Signed approvals** | A networked command refuses (exit 10) until the operator signs that exact plan (`<cli> approve <plan_hash>`). Approvals are single use, expire, and sit in a hash-chained ledger that `<cli> approvals verify` audits. |
| **Careful crawling** | One request at a time, robots.txt obeyed, a politeness ceiling, an egress preflight before any request, tracker blocking over pinned filter lists, and a dedicated Nix-pinned Chromium profile. |
| **Stop on challenge** | A challenge page stops the run (exit 5). A second one inside an hour writes a site cooldown (exit 6). Nothing retries around it. |
| **Gone-storm breaker** | Ad pages that look gone by absence or a redirect (or fail to parse) far more often than chance trip a one-sided binomial rule: the run stops (exit 14), its doubtful gones are quarantined, and the site stays latched until `<cli> breaker clear --approve-operator`. A queue is never re-sent unfiltered: `--supersede` and `crawl resume` admit it through the same gate. |
| **Run cards** | Every run prints a card: stage N/M, inputs, outputs, scale and ETA, and where its live log is. |
| **Append-only store** | Typed JSONL under a data root **outside any git repository**; the CLI refuses a data path inside one. |
| **Gates** | `leak-gate` and `public-gate` (forbidden literals and gitleaks), and `ship`, the one push path. |
| **Claude Code plugin** | Hooks that keep an agent inside the same rules, pinned in `flake.lock`. |

## Quick start

You need [Nix](https://nixos.org/download) with flakes enabled.

```sh
nix develop                     # python + deps, the editable CLI, pinned Chromium, ruff, gitleaks
nix flake check                 # build the package (pytest runs inside it) and every gate
EXAMPLE_DATA="$(mktemp -d)" nix run . -- --help   # the example consumer's CLI (it needs a data root)
nix run .#env-doctor            # which catalogued env vars are set (names only, never values)
nix fmt                         # treefmt: nixfmt + ruff format
```

This repository's own `pipeline.toml` is the **example consumer**
([`examples/example_site`](examples/example_site)): a synthetic site on the
reserved `example.invalid` domain. The harness's tests run against it, so no
test ever reaches a real site.

## Start a consumer

```sh
nix flake init -t github:ismailkattakath/scrape-harness
```

The template ([`templates/default`](templates/default)) is a flake that uses
`scrape-harness.lib`, a source stub, record schemas, tests and a
`pipeline.toml`. Its `FILL-ME` values **refuse to load** until you replace
them, and the refusal names every one. Then:

1. Write your source plug-in and its schemas.
2. Set the data and state roots (outside any repository) in `.envrc.local`.
3. Run `<cli> claude-settings --write` to render the agent settings from
   `pipeline.toml`.
4. `nix flake check`.

[listing-lens](https://github.com/ismailkattakath/listing-lens) is a complete
consumer: every site specific lives outside its repository, and its tests run
on a synthetic site.

## fetch-ads: which ad pages get a view

Every ad GET adds a view to a real person's ad, so `fetch-ads` plans only
ads that pass the **fresh-sighting gate**:

- **alive** in the liveness ledger: not tombstoned, quarantined or held;
- **freshly sighted** by a good, non-sponsored list card;
- **due**: `new`, `card_changed` or `stale_7d`.

Ads that were only sighted long ago stay **dormant**: they get 0 GETs and
nothing is deleted. `--max-fetches` (B) is a **ceiling**, filled by two lanes:

- **revisits** are hard-capped at ceil(rho * B);
- **new** ads get the rest.

Unused revisit quota flows to new ads, and never the other way. The tunables
sit in one signed `SyncPolicy`. A policy change after an approval refuses
(exit 10). `--supersede` and `crawl resume` admit a queued request through
the same gate. The run doc's `ads{}` records the selection, and the run card's
Selection row shows it. See [docs/sync.md](docs/sync.md).

## refresh: the whole chain under one approval

`<cli> refresh` runs `[crawl.refresh].steps` in order under ONE bounded
approval. It is a **strict chain**: a failed step stops every step after it.

- A **crawl-lists step passes only** when all of these hold:
  - it exited 0;
  - its run doc has `list.status: ok`;
  - it wrote at least one observation.
- Anything else is recorded as **exit 14**. That is terminal: the approval is
  spent, and fetch-ads never runs on a list read that cannot be trusted.
- Inside a refresh, "fresh" means sighted by a list step of **this** refresh
  that passed.
- The end summary shows each list step's `list{}` and each fetch-ads step's
  selection: eligible, its lanes and excluded.

| Ends | Exit codes |
|---|---|
| **Terminal** (approval spent) | 3 (a bound breach), 5 and 6 (a challenge or cooldown), 14 (a parse or yield stop, a breaker trip, a failed list postcondition), expiry, revocation, `--abandon` |
| **Resumable** (same run id, same approval) | 4, 7, 8, 10, 12, 13, a crash |

## Flake outputs

| Output | Use |
|---|---|
| `packages.default` / `apps.default` | the example consumer's CLI |
| `packages.plugin` | the Claude Code plugin at the pinned revision |
| `apps.env-doctor`, `apps.leak-gate`, `apps.public-gate`, `apps.ship` | the consumer tools |
| `lib` | for a consumer flake: `python`, `pyDeps`, `browsers`, `filterLists`, `wrapperArgs`, `sshKeyTools`, `cliSmoke`, `consumerTools` |
| `templates.default` | the consumer skeleton |
| `checks` | package (pytest), ruff, formatting, CLI smoke, schema and template gates, toolchain-complete |

Systems: `aarch64-darwin` (the target), `aarch64-linux` and `x86_64-linux`.

## No data here

This repository holds code, schemas and generic configuration only. Crawled
pages, records, logs and approvals live under the consumer's data and state
roots, outside any git tree. `.gitignore` refuses the common data formats as a
second line.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Please read the
[Code of Conduct](CODE_OF_CONDUCT.md). Report security issues privately, as
[SECURITY.md](SECURITY.md) describes.

## Licence

[MIT](LICENSE) © 2026 Ismail Kattakath
