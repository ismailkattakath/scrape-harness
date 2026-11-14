# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The version is the one in `pyproject.toml`.

## [Unreleased]

## [0.1.0] - 2026-11-13

### Added

- Initial public release: the site-agnostic harness for careful,
  approval-gated scraping pipelines. The `pipeline.v1` contract, signed
  single-use approvals with a hash-chained ledger, the egress preflight,
  politeness ceiling and stop-on-challenge crawling, run cards, the
  append-only store, `leak-gate`, `public-gate` and `ship`, the Claude Code
  plugin pin, the synthetic example consumer (`examples/example_site`) and
  the consumer template (`templates/default`).
- Community files (licence, code of conduct, contributing guide, security
  policy, issue and pull request templates) and CI: `gitleaks` over the whole
  history and `nix flake check`, behind one required `gate` check.

[Unreleased]: https://github.com/ismailkattakath/scrape-harness/commits/main
[0.1.0]: https://github.com/ismailkattakath/scrape-harness/commit/873de43
