## What and why

<!-- What changed, and the reason. "What" is visible in the diff; "why" is not.
     If a number justified the change (a timing, a count, a failing case), include it. -->

Closes #

## Checklist

- [ ] `nix flake check` passes locally (it builds the package, so pytest runs inside it)
- [ ] `nix fmt` leaves no diff
- [ ] Every commit is **signed** (`git log --format='%h %G?'` shows `G`)
- [ ] `CHANGELOG.md` updated under `## [Unreleased]`
- [ ] **No data:** no crawled pages, records, logs, real URLs, names or coordinates —
      tests use synthetic fixtures only
- [ ] No secret values, no personal paths, no email addresses

### If this touches networked behaviour

- [ ] Every request still goes through the approval, egress preflight and politeness checks
- [ ] A challenge still **stops** the run; nothing retries around it
- [ ] The change is covered by an offline test (the Nix sandbox has no network)

## Testing

<!-- Which system you ran `nix flake check` on, and anything you checked by hand. -->

- System:
- Checked by hand:
