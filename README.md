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
| **Preflight** | `<cli> preflight refresh` (or any command) checks, before the Touch ID, that each secret env var a step declares in `secrets_env` is set (by name only), and that the signers file, cooldown and breaker latch are clear. `refresh` does the secret check itself first. See [docs/preflight.md](docs/preflight.md). |
| **Claude Code plugin** | Hooks that keep an agent inside the same rules, pinned in `flake.lock`. |
| **CLI spec** | `<cli> cli spec` prints every command and flag (the consumer's own plug-in commands too) as `cli.v1` JSONL, with each command's network class from `pipeline.toml`. `--format md` renders a reference ([docs/cli.md](docs/cli.md) is the example's); `--check FILE` fails on drift. A site-overlay consumer renders from its repo `pipeline.toml`, never the site file. A plug-in command takes the network flags from `cli.network_args(sp)`, or only `--approve-network` from `cli.approve_network_arg(sp)`. |

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

## crawl-lists: how a list is read

| Mode | Reads | Ends `completed` at |
|---|---|---|
| none (a sample) | page 1, then the pages it plans from the page count | the planned pages (`ok`) |
| `--pages 1,2,9` | exactly these pages | the named pages (`ok`) |
| `--mode walk` (the census) | 1, 2, 3, ..., one page ahead | a positive END (`end`), or its ceiling (`ceiling_hit`) |
| `--mode frontier` | 1, 2, 3, ..., one page ahead | a confirmed anchor (`anchored`), a positive END, or its ceiling (`ceiling_hit`, with a GAP) |

- A **positive END** is the site's own "no results" page: the source's
  `end_selectors` match, 0 cards are parsed, and the page was not redirected.
- `--max-pages` is **every list GET** of a walk, the END page included.
  `--min-pages` (census only) makes an END before that page exit 14.
- `mode`, `min_pages`, a frontier's `k` and the policy digest are signed in
  the plan, and in a refresh's list set.
- A frontier stops early only when the source declares
  `order = "bump_desc"`. See [docs/sync.md](docs/sync.md).

## refresh: the whole chain under one approval

`<cli> refresh` runs `[crawl.refresh].steps` in order under ONE bounded
approval. It is a **strict chain**: a failed step stops every step after it.

- A crawl-lists step may carry `--mode walk|frontier`, `--min-pages` and
  `--max-pages` in its `args`. The bounds sign each step's set and count
  exactly `--max-pages` list GETs for a walk.
- A walk step needs a source that declares `end_selectors`, and a census
  step needs `--min-pages`. Without them `refresh` refuses it at plan time,
  with **exit 2**.
- A **crawl-lists step passes only** when all of these hold:
  - it exited 0;
  - its run doc is of the step's mode, with a passing `list.status`:

    | Step | Passes on |
    |---|---|
    | `--mode walk` | `end` (a clamp included) |
    | `--mode frontier` | `anchored`, `end` or `ceiling_hit` (shown as a GAP) |
    | no mode | `ok`, `end`, `ceiling_hit` or `anchored` |
  - it wrote at least one observation, or, for a walk step, page 1
    positively ENDED (an empty subcategory).
- Anything else is recorded as **exit 14**. That is terminal: the approval is
  spent, and fetch-ads never runs on a list read that cannot be trusted.
- Inside a refresh, "fresh" means sighted by a list step of **this** refresh
  that passed.
- **Which proxy.** With several `[crawl.proxy]` urls, a new plan picks one
  and signs it. `--proxy-only TEXT` makes it pick the one url containing
  TEXT. `--proxy-exclude TEXT` (repeatable) never picks a url containing
  TEXT. The matching is the same substring test as `probe robots`
  (`--proxy-only`) and `crawl-lists` (`--proxy-exclude`). refresh is
  stricter: it applies both flags together, and it checks them with one
  url configured too.
  - `refresh` refuses, before any plan is saved, when the flags leave no url
    or `--proxy-only` matches more or less than one.
  - The bounds sign the chosen url, not the flags. A plan without them
    hashes as before.
  - A live approval of an excluded url is not used.
  - A resume keeps the proxy its approval signed, whatever the flags say.
- The run card has a line per walk step: its mode, its ceiling and what it
  passes on (a frontier's reference and open GAP too).
- The end summary shows each list step's `list{}`, with a walk's mode, pages
  read, `end_page`, anchor and GAP, and each fetch-ads step's selection:
  eligible, its lanes and excluded.

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
| `apps.examplesite`, `apps.preflight`, `apps.cli-spec` | the CLI with the checkout's env files loaded (see below) |
| `lib` | for a consumer flake: `python`, `pyDeps`, `browsers`, `filterLists`, `wrapperArgs`, `sshKeyTools`, `cliSmoke`, `consumerTools` |
| `templates.default` | the consumer skeleton |
| `checks` | package (pytest), ruff, formatting, CLI smoke, schema and template gates, `cli-spec-drift`, `consumer-apps`, toolchain-complete |

### Consumer tools

`scrape-harness.lib.consumerTools` is the one generator of a consumer's
tools. It reads the consumer's `pipeline.toml` `[consumer]` table. The
harness builds its own example consumer's tools with it.

| Argument | Gives |
|---|---|
| `pkgs`, `consumer` | `env-doctor`, `leak-gate`, `public-gate`, `ship` |
| `cliPackage` | `apps.<cli>`, `apps.preflight`, `apps.cli-spec` (and `appPackages`) |
| `cliSpecDoc` (+ `pipeline`) | `checks.cli-spec-drift` |

A consumer merges `apps`, `appPackages` and `checks` into its own outputs.

`nix run .#<cli> -- refresh --dry-run` runs the CLI from any shell:

- It loads `.env`, `.env.local` and `.envrc.local` from the checkout root,
  the files the `.envrc` loads.
- It loads them only when direnv allows the checkout's `.envrc`. A checkout
  never allowed, edited since, or with no `.envrc` is refused (exit 4). Run
  `direnv allow` once. A checkout with none of the files needs no allow.
- A name that `.envrc.local` sets or changes beats an exported one, as in a
  direnv shell. A dotenv file sets only names still unset.
- `.envrc.local` is plain shell, sourced after the dotenv files. direnv's
  own functions are not there.
- It refuses (exit 4) outside a git checkout of this consumer.
- It prints no value.

`nix run .#preflight -- refresh` is `<cli> preflight refresh`.
`nix run .#cli-spec -- --format md` is `<cli> cli spec --format md`.

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
