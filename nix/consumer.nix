# The consumer-facing tools of scrape-harness, as a function of a consumer's pipeline.toml [consumer]
# table: the env catalogue, env-doctor, leak-gate and ship. The harness builds its own example consumer's
# tools with it exactly as a consumer flake does (`scrape-harness.lib.consumerTools`), so there is one copy.
{
  lib,
  pkgs,
  consumer,
  extraCatalogue ? { }, # the consumer's own variables (e.g. an API key), same shape
  plugin ? "./plugin", # the plugin `ship`'s live smoke loads: the working tree's, or the harness's store path
  shipLocalChecks ? [ ], # shell commands `ship` runs on the HOST before the push (see ship-local-checks)
  public ? false, # true: the repo is (or becomes) public, so `ship` also runs public-gate (see below)
}:
let
  knob = n: "${consumer.env_prefix}_${n}";
  # Env catalogue — the single list of every variable. need: required | optional | local.
  # secret = true → tooling reports presence only.
  envCatalogue = {
    ${consumer.data_env} = {
      need = "required";
      note = "Data folder (Google Drive), outside any git repo. The CLI refuses a path inside one.";
    };
    ${consumer.state_env} = {
      need = "optional";
      note = "Local machine state (browser profile, queues, approvals); default $XDG_STATE_HOME/${consumer.state_dirname}. Never a sync folder.";
    };
    ${knob "HOME_COUNTRY"} = {
      need = "optional";
      note = "Egress preflight: crawls start only if Cloudflare's trace reports this country (default CA).";
    };
    ${knob "EGRESS_TRACE_URL"} = {
      need = "optional";
      note = "Egress preflight endpoint (default https://www.cloudflare.com/cdn-cgi/trace; never the target site).";
    };
    ${knob "APPROVAL_SIGNERS"} = {
      need = "required";
      note = "allowed_signers file (outside any repo) of the approval key; approve/verify refuse without it.";
    };
    ${knob "APPROVAL_AGENT"} = {
      need = "optional";
      note = "SSH agent socket that signs approvals (default: Secretive's; Touch ID per signature).";
    };
    ${knob "SHIP_NO_SMOKE"} = {
      need = "optional";
      note = "1 = `nix run .#ship` skips the live Claude Code smoke run (only where claude is absent).";
    };
    ${knob "NO_NOTIFY"} = {
      need = "optional";
      note = "1 = no macOS notification with the plan summary before the Touch ID prompt.";
    };
    ${knob "NO_CLIPBOARD"} = {
      need = "optional";
      note = "1 = do not copy the run card's tail line to the clipboard (pbcopy).";
    };
    ${knob "CHROMIUM"} = {
      need = "optional";
      note = "Chromium executable override (default: the Nix-pinned Playwright Chromium).";
    };
    ${knob "VARIANT"} = {
      need = "optional";
      note = "Page variant: desktop (default) or mobile; `${consumer.cli} ab` compares them.";
    };
    ${knob "NETWORK_APPROVED"} = {
      need = "optional";
      note = "1 = the operator approved this networked run (same as --approve-network).";
    };
    ${knob "FORBIDDEN_LITERALS"} = {
      need = "local";
      note = "File (outside the repo) of literals that must never land here; leak-gate + hook.";
    };
    ${knob "PUBLIC_LITERALS"} = {
      need = "local";
      note = "Files (a:b, outside the repo) of every literal a PUBLIC tree must not hold; public-gate.";
    };
    ${knob "PUBLIC_ALLOW"} = {
      need = "local";
      note = "File (outside the repo) of exact strings public-gate cuts out before matching (e.g. stored method names).";
    };
  };
  envCatalogueAll = envCatalogue // extraCatalogue;
  envTsv = lib.concatMapStringsSep "\n" (
    n:
    let
      e = envCatalogueAll.${n};
    in
    "${n}\t${e.need}\t${if e.secret or false then "secret" else "plain"}\t${e.note}"
  ) (lib.attrNames envCatalogueAll);
in
rec {
  inherit knob;
  envCatalogue = envCatalogueAll;
  env-doctor = pkgs.writeShellApplication {
    name = "env-doctor";
    text = ''
      missing=0
      printf '%-26s %-9s %-7s %s\n' NAME NEED KIND STATE
      while IFS="$(printf '\t')" read -r name need kind note; do
        if [ -n "''${!name:-}" ]; then state="set"; else state="MISSING"; fi
        [ "$state" = MISSING ] && [ "$need" = required ] && missing=1
        printf '%-26s %-9s %-7s %-8s %s\n' "$name" "$need" "$kind" "$state" "$note"
      done <<'EOF'
      ${envTsv}
      EOF
      exit "$missing"
    '';
  };

  # The consumer's HOST-side checks (the shipLocalChecks argument): what the Nix sandbox cannot see,
  # e.g. a test that reads the DATA root. A Nix argument like extraCatalogue, not a pipeline.toml key, so
  # neither the pipeline schema nor the auto-updating plugin's guard changes. Run with the operator's environment, in order; the first
  # non-zero exit refuses the push (3, like ship's other refusals) and the rest do not run. Absent or
  # empty = a silent no-op, so a consumer that declares none is unchanged.
  ship-local-checks = pkgs.writeShellApplication {
    name = "ship-local-checks";
    runtimeInputs = [ pkgs.bash ];
    text =
      lib.concatMapStrings (c: ''
        echo "== local check: "${lib.escapeShellArg c}
        rc=0; bash -c ${lib.escapeShellArg c} || rc=$?
        if [ "$rc" -ne 0 ]; then
          echo "ship: refused, a local check failed (exit $rc): "${lib.escapeShellArg c} >&2
          exit 3
        fi
      '') shipLocalChecks
      + "true\n";
  };

  # The ONE push path (folded in from listing-insights' `nix run .#ship`). A global core.hooksPath
  # replaces repo hooks, so a pre-push hook cannot be relied on; this is run instead of `git push`.
  # Every gate must pass, in this order, or nothing is pushed (exit 3 = refused before any check).
  ship = pkgs.writeShellApplication {
    name = "ship";
    runtimeInputs = [
      pkgs.git
      pkgs.gnugrep
      pkgs.coreutils
      leak-gate
      ship-local-checks
      public-gate
    ];
    text = ''
      refuse() { echo "ship: $*" >&2; exit 3; }
      [ "$(git rev-parse --abbrev-ref HEAD)" = main ] || refuse "not on main"
      [ -z "$(git status --porcelain)" ] || refuse "uncommitted or untracked changes"
      # An EMPTY remote (a new repo) has no main yet: `ls-remote --exit-code` answers 2 for exactly that, and
      # anything else (a network or auth error) is a refusal, never mistaken for "empty".
      heads=0; git ls-remote -q --exit-code --heads origin main >/dev/null || heads=$?
      if [ "$heads" -eq 0 ]; then
        git fetch -q origin main
        git merge-base --is-ancestor origin/main HEAD || refuse "rebase onto origin/main first"
        range="origin/main..HEAD"
      elif [ "$heads" -eq 2 ]; then
        echo "== the remote has no main yet: a first push of every commit on HEAD"
        range="HEAD"
      else
        refuse "cannot read the remote (git ls-remote exit $heads)"
      fi
      [ -n "$(git rev-list "$range")" ] || refuse "nothing to push"
      unsigned="$(git log --format='%h %G?' "$range" | grep -v ' G$' || true)"
      [ -z "$unsigned" ] || refuse "unsigned or unverifiable commits: $unsigned"
      echo "== leak-gate"; leak-gate
      ${lib.optionalString public ''echo "== public-gate"; public-gate''}
      echo "== nix flake check"; nix flake check
      if [ "$(uname -s)" = Darwin ]; then
        # Each leg's result is a GC ROOT (nix's own --out-link, overwritten per ship; the nix-direnv
        # pattern): with --no-link, the daemon's automatic GC (every 1-2 h on a full disk) collected the
        # closures, and the paths no binary cache has (spacy, browsers, Rust builds) were rebuilt, >20 min.
        roots="''${XDG_STATE_HOME:-$HOME/.local/state}/${consumer.state_dirname}/gcroots"
        mkdir -p "$roots"
        # flake check just built it, so --rebuild re-runs pytest instead of serving a cached pass
        echo "== aarch64-darwin tests, rebuilt"; nix build --out-link "$roots/darwin-check" --rebuild .#checks.aarch64-darwin.default
        echo "== aarch64-linux package (pytest in the sandbox)"; nix build --out-link "$roots/linux-package" .#packages.aarch64-linux.default
      fi
      ship-local-checks   # the consumer's host-side checks; exits 3 on the first failure
      [ -d "${plugin}" ] || refuse "no plugin at ${plugin} for the live smoke"
      if [ -d "${plugin}" ]; then
        if command -v claude >/dev/null; then
          echo "== live smoke: Claude Code loads the plugin and the settings with no warning"
          err="$(mktemp)"; trap 'rm -f "$err"' EXIT
          out="$(timeout 120 claude -p "Reply with the single word ok." --plugin-dir "${plugin}" --max-turns 1 2>"$err")" \
            || { cat "$err" >&2; refuse "the live smoke run failed"; }
          if grep -q -i "is not matched\|error" "$err"; then cat "$err" >&2; refuse "Claude Code warned at load"; fi
          [ -n "$out" ] || refuse "the live smoke run printed nothing"
        elif [ "''${${knob "SHIP_NO_SMOKE"}:-}" = 1 ]; then
          echo "== live smoke SKIPPED (${knob "SHIP_NO_SMOKE"}=1)"
        else
          refuse "claude is not on PATH for the live smoke (set ${knob "SHIP_NO_SMOKE"}=1 to skip it knowingly)"
        fi
      fi
      echo "== push"; git push origin HEAD:main
      echo "ship: pushed $(git rev-parse --short HEAD)"
    '';
  };

  # The PUBLIC tree check: what a push would publish (`git archive HEAD`, every tracked file, flake.lock
  # included) plus HEAD's message and author, against ${knob "PUBLIC_LITERALS"}, with NO persona allowance
  # (a public repo names no persona at all). Only the exact strings in ${knob "PUBLIC_ALLOW"} are cut out
  # first. A compressed file refuses (it cannot be inspected). Prints file:line, never the literal.
  public-gate = pkgs.writeShellApplication {
    name = "public-gate";
    runtimeInputs = [
      pkgs.git
      pkgs.gnutar
      pkgs.gitleaks
      pkgs.perl
      pkgs.coreutils
    ];
    text = ''
      lits="''${${knob "PUBLIC_LITERALS"}:-}"
      [ -n "$lits" ] || { echo "public-gate: ${knob "PUBLIC_LITERALS"} must name readable files (a:b), outside the repo" >&2; exit 2; }
      tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
      IFS=: read -r -a files <<<"$lits"
      for f in "''${files[@]}"; do
        [ -r "$f" ] || { echo "public-gate: a listed literals file is unreadable; failing closed" >&2; exit 2; }
        cat "$f" >> "$tmp/pat"; echo >> "$tmp/pat"
      done
      allow="''${${knob "PUBLIC_ALLOW"}:-}"
      if [ -n "$allow" ] && [ ! -r "$allow" ]; then echo "public-gate: ${knob "PUBLIC_ALLOW"} is unreadable" >&2; exit 2; fi
      mkdir "$tmp/tree"; git archive HEAD | tar -x -C "$tmp/tree"
      git log -1 --format='%an %ae%n%B' HEAD > "$tmp/msg"
      n="$(find "$tmp/tree" -type f | wc -l | tr -d ' ')"
      gitleaks dir --redact --no-banner "$tmp/tree"
      if ! out="$(perl ${./public-gate.pl} "$tmp/pat" "$allow" "$tmp/tree" "$tmp/msg")"; then
        echo "public-gate: FAILED, a forbidden literal (or an uninspectable file) at (file:line; the literal is not printed):" >&2
        printf '%s\n' "$out" >&2
        exit 1
      fi
      echo "public-gate: clean ($n files of HEAD's tree, and its message/author)"
    '';
  };

  leak-gate = pkgs.writeShellApplication {
    name = "leak-gate";
    runtimeInputs = [
      pkgs.git
      pkgs.gitleaks
      pkgs.gnugrep
      pkgs.coreutils
      pkgs.perl
    ];
    text = ''
      # Fails CLOSED: no literal list, no push. The list lives outside the repo on purpose —
      # a list of forbidden strings committed here would itself be the leak.
      # A colon-separated LIST of files (identity strings, site words), each kept once outside the
      # repo. Fails CLOSED: unset, or ANY listed file unreadable, or no pattern at all -> no push.
      lits="''${${knob "FORBIDDEN_LITERALS"}:-}"
      if [ -z "$lits" ]; then
        echo "leak-gate: ${knob "FORBIDDEN_LITERALS"} must name one or more readable files (a:b), outside the repo" >&2
        exit 2
      fi
      if git rev-parse -q --verify origin/main >/dev/null; then range="origin/main..HEAD"; else range="HEAD"; fi
      pat="$(mktemp)"; trap 'rm -f "$pat"' EXIT
      IFS=: read -r -a files <<<"$lits"
      for f in "''${files[@]}"; do
        [ -r "$f" ] || { echo "leak-gate: a listed literals file is unreadable; failing closed" >&2; exit 2; }
        grep -v '^[[:space:]]*$' "$f" >> "$pat" || true   # an empty pattern would match everything
      done
      [ -s "$pat" ] || { echo "leak-gate: literal list is empty" >&2; exit 2; }
      gitleaks git --log-opts="$range" --redact --no-banner .
      # The persona-check allowlist, from the SAME source (git config persona.allow, an ERE set in the
      # persona include): its matches are cut out of each line BEFORE the forbidden match, so an allowed
      # form passes while the name, the email or a line mixing both still fails. Unset: no allowance.
      allow="$(git config --get persona.allow || true)"
      strip() { if [ -n "$allow" ]; then ALLOW="$allow" perl -pe 's/$ENV{ALLOW}//gi'; else cat; fi; }
      hits=0
      tree="$(git grep -n -I -i -F -f "$pat" -- . ':!flake.lock' | strip | grep -i -F -f "$pat" || true)"
      if [ -n "$tree" ]; then
        echo "leak-gate: a forbidden literal is in the tree at (file:line; the literal is not printed):" >&2
        printf '%s\n' "$tree" | cut -d: -f1-2 >&2
        hits=1
      fi
      if git log -p --format='%an %ae%n%B' "$range" | strip | grep -q -i -F -f "$pat"; then
        echo "leak-gate: a forbidden literal is in the commits of $range (messages, authors or diffs)" >&2
        hits=1
      fi
      [ "$hits" -eq 0 ] || { echo "leak-gate: FAILED — do not push" >&2; exit 1; }
      echo "leak-gate: clean ($range)"
    '';
  };
}
