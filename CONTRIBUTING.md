# Contributing

Thanks for helping. This harness exists to make scraping **careful**, so a
change is judged first on whether every safety check still holds, and only
then on what it adds.

## Quick start

You need [Nix](https://nixos.org/download) with flakes enabled. Everything
else (Python and its dependencies, the pinned Chromium, ruff, gitleaks) comes
from the flake.

```sh
git clone https://github.com/ismailkattakath/scrape-harness
cd scrape-harness
nix develop          # or `direnv allow` once
git add -A           # flakes ignore untracked files: stage new files before any nix command
nix flake check      # the whole suite
nix fmt              # format before you commit
```

## Building and testing

- **`nix flake check` is the test suite.** It builds the package, and pytest
  runs *inside* that build, so the package cannot exist with a red test. It
  also runs ruff, the formatting check, the CLI smoke test, the schema gates
  and the template gates.
- **Tests are offline.** On Linux the Nix sandbox has no network. Tests run
  against the synthetic example site (`examples/example_site`, on the reserved
  `example.invalid` domain) and local fixtures. A test that needs a real site
  will not be accepted.
- **Faster loop:** `nix develop -c pytest -q` runs the tests in the dev shell.
  Finish with `nix flake check` all the same.
- **Formatting:** `nix fmt` (nixfmt + ruff format). The `formatting` check
  fails on any diff.

CI runs the same `nix flake check` on a GitHub-hosted Linux runner, plus
gitleaks over the whole history. The one required check is `gate`.

## No data in this repository

This repository holds code, schemas and generic configuration only. It is
public, and so is every commit in its history.

- **Never commit** crawled pages, records, logs, run state, approvals, real
  URLs, names, phone numbers or coordinates.
- Data and state live under the roots named in `pipeline.toml` `[consumer]`,
  **outside any git repository**. The CLI refuses a data path inside one.
- **Fixtures are synthetic.** Invent them; never trim a real page.
- No secret values, no personal paths, no email addresses.

## Commits

- **Sign every commit** (SSH or GPG signing). `git log --format='%h %G?'` must
  show `G` for each one. `nix run .#ship` refuses unsigned commits; please sign the commits in a PR too.
- One logical change per commit. The subject says what changed; the body says
  why, with the number or case that justified it.
- Update `CHANGELOG.md` under `## [Unreleased]`.

## Pull requests

1. Fork, branch, commit (signed), push to your fork.
2. Open a pull request against `main` and fill in the template.
3. CI runs `gitleaks` and `nix flake check`; `gate` must be green.
4. The maintainer reviews and merges.

## The gates a change faces

| Gate | Where it runs | What it refuses |
|---|---|---|
| `gate` (CI) | every pull request and push to `main` | a red `gitleaks` or `nix flake check` |
| `gitleaks` (CI) | the whole git history | secrets, even ones a later commit removed |
| `leak-gate` | the maintainer's machine, before a push | gitleaks over unpushed commits, plus a private list of forbidden literals kept **outside** the repository |
| `public-gate` | the maintainer's machine, before a push | gitleaks over the exported tree, plus forbidden literals in any tracked file or in the commit message and author |

The literal lists are private on purpose: a list of strings that must never
appear here would itself be the leak. Without one, both gates **fail closed**
(exit 2), so contributors rely on CI and review instead.

## How changes land: `nix run .#ship`

The maintainer pushes to `main` only through `nix run .#ship`, never a bare
`git push`. It stops at the first failure:

1. on `main`, with a clean tree, rebased on `origin/main`;
2. every unpushed commit signed;
3. `leak-gate`, then `public-gate`;
4. `nix flake check`, then (on macOS) the darwin tests rebuilt and the
   `aarch64-linux` package built;
5. a live Claude Code smoke run of the plugin;
6. `git push`.

`ship` needs the maintainer's literal lists and signing key, so it is not a
contributor tool. A merged pull request goes through the same CI.

## Reporting

- **Bugs and features:** use the issue forms.
- **Security problems:** report privately, as [SECURITY.md](SECURITY.md) says.
- **Conduct:** this project follows the [Code of Conduct](CODE_OF_CONDUCT.md).

By contributing you agree that your contribution is licensed under the
[MIT License](LICENSE).
