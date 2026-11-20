# scrape-harness — the generic core of a polite, typed scraping pipeline (extracted from listing-insights).
#
#   nix develop          → python (+ deps, the editable CLI), Nix-pinned Chromium, ruff, check-jsonschema, gitleaks
#   nix run              → the consumer's CLI (packages.default; named by pipeline.toml [consumer].cli)
#   nix run .#env-doctor → which catalogued env vars are set (NAMES only, never values)
#   nix run .#leak-gate  → gitleaks + forbidden-literal grep over unpushed commits; run before push
#   nix fmt              → treefmt (nixfmt + ruff format)
#   nix flake check      → package build (runs pytest, incl. hook tests), ruff, schema gates,
#                          formatting, toolchain-complete
#
# Shape follows the nix-dev-toolkit template (treefmt-nix formatter + check, one devPackages list
# read by both the shell and `toolchain-complete`, an env catalogue). Its Postgres stack is dropped:
# this project stores append-only JSONL, so a database would be a second copy of the data.
{
  description = "scrape-harness: the generic core of a polite, typed scraping pipeline";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    treefmt-nix = {
      url = "github:numtide/treefmt-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    # The Claude Code plugin lives in the ONE marketplace repo (the input below)
    # (plugins/scrape-harness). Pinned here: the harness's tests, ship's live smoke and every consumer's
    # packages.plugin use THIS rev, while interactive sessions load the auto-updating marketplace copy
    # (the guard refuses a plugin/harness version mismatch in either direction).
    kattakath-skills = {
      url = "github:kattakath/skills";
      flake = false;
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      treefmt-nix,
      kattakath-skills,
    }:
    let
      inherit (nixpkgs) lib;
      # darwin is the target; aarch64-linux stays eval-clean; x86_64-linux is what hosted CI runners
      # usually are, so a CI can run `nix flake check` at all.
      systems = [
        "aarch64-darwin"
        "aarch64-linux"
        "x86_64-linux"
      ];
      forAll = f: lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      # pyproject.toml is the ONE place the version, the CLI entry point and the plug-in entry points
      # are declared; the editable dev package reads them from it instead of restating them.
      pyproject = lib.importTOML ./pyproject.toml;
      # pipeline.toml [consumer] is the ONE place the CLI name, env names and state folder are
      # declared; the package, the env catalogue and the shell read them from it.
      consumer = (lib.importTOML ./pipeline.toml).consumer;
      scripts = {
        ${consumer.cli} = pyproject.project.scripts.scrape-harness;
      };
      extraPy = pkgs: ps: import ./nix/python-packages.nix { inherit pkgs ps; };
      pyDepsFor =
        pkgs: ps:
        let
          x = extraPy pkgs ps;
        in
        [ x.crawlee ]
        ++ (with ps; [
          beautifulsoup4
          rich # the run card (Panel + Table) on stderr
          jsonschema
          rfc3339-validator
          rfc3986-validator
          extruct # site-agnostic schema.org microdata / JSON-LD (pins, datePublished)
          playwright # driven by crawlee's PlaywrightCrawler; also resolves the pinned Chromium path
          doit # `<cli> run`: the declared stages as tasks (order, up-to-date checks), from pipeline.toml
          adblock # Brave's adblock-rust: tracker blocking over the pinned EasyList + EasyPrivacy (trackers.py)
        ]);
      # The Nix-pinned Chromium (Chrome for Testing, hash-pinned per system). nixpkgs' own
      # `chromium` does not build on aarch64-darwin; this matches the pinned Playwright's revision.
      browsersFor = pkgs: pkgs.playwright-driver.browsers-chromium;

      # EasyList + EasyPrivacy for tracker blocking (trackers.py), pinned by a commit of easylist/easylist's
      # gh-pages branch plus sha256: the CDN URL (easylist.to) serves whatever is current, so it drifts.
      # Refresh = bump `rev` and both hashes (`nix store prefetch-file <url>`); the test that counts rules
      # the adblock 0.6.0 parser ignores then says whether the new lists still fit the pinned engine.
      filterListsRev = "023c73844f1d6e66a304ea2b6137b99edc3b251c"; # gh-pages, 2026-11-08T16:42:37Z
      filterListsFor =
        pkgs:
        let
          get =
            name: hash:
            pkgs.fetchurl {
              url = "https://raw.githubusercontent.com/easylist/easylist/${filterListsRev}/${name}";
              inherit hash;
            };
        in
        pkgs.linkFarm "easylist-${builtins.substring 0 8 filterListsRev}" [
          {
            name = "easylist.txt";
            path = get "easylist.txt" "sha256-mp40ambAw1sIv0bQ9Dq/4/dNTHkBjsT24Bs4vdrNIdA=";
          }
          {
            name = "easyprivacy.txt";
            path = get "easyprivacy.txt" "sha256-FWIvqK8QKo98016iFkb50HjLwcgaAAXCovn/jzcjQC0=";
          }
        ];

      src = lib.fileset.toSource {
        root = ./.;
        fileset = lib.fileset.unions [
          ./pyproject.toml
          ./pipeline.toml
          ./src
          ./examples
          ./schemas
          ./tests
          ./docs/cli.md # generated by `cli spec --format md`; a test checks it is in sync
          ./.claude
          ./nix/consumer.nix # the ship app's source: a test pins its build legs as GC roots
        ];
      };

      li =
        pkgs:
        pkgs.python3Packages.buildPythonApplication {
          pname = pyproject.project.name;
          inherit (pyproject.project) version;
          pyproject = true;
          inherit src;
          build-system = [ pkgs.python3Packages.hatchling ];
          dependencies = pyDepsFor pkgs pkgs.python3Packages;
          # pytest runs inside the build: the package cannot exist with a red test. Offline by
          # construction on Linux (sandbox); see docs/design.md for the darwin sandbox check.
          # the tests drive the plugin's hooks at the pinned rev
          # tests/test_blocking.py launches the SAME pinned Chromium the wrapper uses, on 127.0.0.1 only
          preCheck = ''
            export SCRAPE_HARNESS_PLUGIN=${self.packages.${pkgs.stdenv.hostPlatform.system}.plugin}
            export PLAYWRIGHT_BROWSERS_PATH=${browsersFor pkgs}
            export SCRAPE_HARNESS_FILTER_LISTS=${filterListsFor pkgs} SCRAPE_HARNESS_FILTER_LISTS_PIN=${filterListsRev}
            # crawlee launches Chromium WITH its own sandbox (chromium_sandbox = not this), as crawls do;
            # the build user cannot initialise it (darwin _nixbld: "sandbox initialization failed", then
            # SEGV), so only the build's tests turn it off. The wrapper and the dev shell never set it.
            export CRAWLEE_DISABLE_BROWSER_SANDBOX=true
            # Chromium's crash reporter keeps its database under HOME; the build's HOME (/homeless-shelter)
            # does not exist, so chrome_crashpad_handler exits ("--database is required") and Chromium
            # aborts at launch (measured on a GitHub ubuntu-24.04-arm runner). A writable HOME fixes it.
            export HOME="$TMPDIR"
          '';
          nativeCheckInputs = [
            pkgs.python3Packages.pytestCheckHook
            pkgs.git # test_data_dir_inside_a_repo_is_refused runs `git init`
            pkgs.python3Packages.wcwidth # the run-card width test measures with it, not with rich
            pkgs.python3Packages.hypothesis # property tests of the ad selector (tests/test_select_ads.py)
            pkgs.openssh # ssh-keygen -Y sign/verify: the approval signatures
            pkgs.nodejs # drives plugin/workflows/plan-pipeline.js with mocked agents (tests/test_workflow.py)
          ];
          makeWrapperArgs = wrapperArgsFor {
            inherit pkgs consumer;
            rev = self.shortRev or self.dirtyShortRev or "unknown";
          };
          # the generic entry point, installed under the consumer's CLI name
          postInstall = lib.optionalString (consumer.cli != "scrape-harness") ''
            mv "$out/bin/scrape-harness" "$out/bin/${consumer.cli}"
          '';
          meta.mainProgram = consumer.cli;
        };

      devPackagesFor = pkgs: [
        (pkgs.python3.withPackages (
          ps:
          pyDepsFor pkgs ps
          ++ [
            ps.pytest
            ps.wcwidth
            ps.hypothesis
            (ps.mkPythonEditablePackage {
              pname = pyproject.project.name;
              inherit (pyproject.project) version;
              inherit scripts;
              entry-points = pyproject.project.entry-points;
              root = "$PRJ_ROOT/src"; # expanded at runtime; set by the shellHook
              dependencies = pyDepsFor pkgs ps;
            })
          ]
        ))
        pkgs.ruff
        pkgs.check-jsonschema
        pkgs.gitleaks
        pkgs.git
        (sshKeyToolsFor pkgs) # ssh-keygen -Y: approval signatures (never `ssh` itself)
        pkgs.nodejs # the plan-pipeline workflow's tests
        pkgs.jq
      ];
      requiredBins = [
        "python3"
        consumer.cli
        "pytest"
        "ruff"
        "check-jsonschema"
        "gitleaks"
      ];

      consumerTools =
        pkgs:
        import ./nix/consumer.nix {
          inherit lib pkgs consumer;
          plugin = "${self.packages.${pkgs.stdenv.hostPlatform.system}.plugin}";
          public = true; # a public repo: ship also runs public-gate
        };

      # ssh-keygen / ssh-agent / ssh-add ONLY (approval signatures and their tests), never `ssh`: Nix's
      # ssh rejects macOS's `UseKeychain` in ~/.ssh/config, so putting it on PATH breaks git over SSH.
      sshKeyToolsFor =
        pkgs:
        pkgs.runCommandLocal "ssh-key-tools" { } ''
          mkdir -p "$out/bin"
          for t in ssh-keygen ssh-agent ssh-add; do ln -s ${pkgs.openssh}/bin/$t "$out/bin/$t"; done
        '';

      # The CLI wrapper's environment, for the harness's own package and a consumer's alike.
      # GOES RED WHEN: the BUILT CLI cannot start against its own pipeline.toml: `<cli> --help` and each
      # `<cli> <command> --help` run from the package, with a hostile PYTHONPATH whose scrape_harness
      # raises on import (so a CLI that is shadowed by an ambient path fails here, not on the operator).
      cliSmokeFor =
        {
          pkgs,
          package,
          consumer,
          commands ? [ ],
          pipeline ? null, # a consumer's package embeds its own; the harness's example CLI does not
        }:
        pkgs.runCommandLocal "cli-smoke" { } ''
          ${lib.optionalString (pipeline != null) "export SCRAPE_HARNESS_PIPELINE=${pipeline}"}
          mkdir -p hostile/scrape_harness data state
          echo 'raise ImportError("an ambient PYTHONPATH shadowed the pinned scrape_harness")' > hostile/scrape_harness/__init__.py
          export HOME="$PWD" PYTHONPATH="$PWD/hostile" ${consumer.data_env}="$PWD/data" ${consumer.state_env}="$PWD/state"
          ${package}/bin/${consumer.cli} --help > /dev/null
          for c in ${lib.escapeShellArgs commands}; do ${package}/bin/${consumer.cli} $c --help > /dev/null; done
          touch "$out"
        '';

      wrapperArgsFor =
        {
          pkgs,
          consumer,
          rev,
          pipeline ? null, # a consumer's pipeline.toml in the store: the installed CLI's default
        }:
        [
          # The installed CLI runs ITS pinned harness, never one from an ambient PYTHONPATH: a dev shell
          # exports its own (measured 2026-11-08: a shell on an older lock made `li` load an older harness,
          # whose pipeline.v1 rejected the repo's own [crawl] section).
          "--unset PYTHONPATH"
          "--unset PYTHONHOME"
          "--set-default PLAYWRIGHT_BROWSERS_PATH ${browsersFor pkgs}"
          "--set-default PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS true"
          "--set-default SCRAPE_HARNESS_FILTER_LISTS ${filterListsFor pkgs}" # tracker blocking's pinned lists
          "--set-default SCRAPE_HARNESS_FILTER_LISTS_PIN ${filterListsRev}"
          "--suffix PATH : ${sshKeyToolsFor pkgs}/bin" # ssh-keygen for approvals; a system one wins if present
          # the CLI build's git revision for the run card; a dirty tree says so
          "--set-default ${consumer.env_prefix}_BUILD_REV ${rev}"
        ]
        ++ lib.optional (pipeline != null) "--set-default SCRAPE_HARNESS_PIPELINE ${pipeline}";

      # The harness as a Python LIBRARY, for a consumer's dependencies (its CLI is the consumer's).
      pythonFor =
        pkgs: ps:
        ps.buildPythonPackage {
          pname = pyproject.project.name;
          inherit (pyproject.project) version;
          pyproject = true;
          src = lib.fileset.toSource {
            root = ./.;
            fileset = lib.fileset.unions [
              ./pyproject.toml
              ./src
              ./schemas
            ];
          };
          build-system = [ ps.hatchling ];
          dependencies = pyDepsFor pkgs ps;
          doCheck = false; # the harness's own suite runs in packages.default; a consumer runs its own
          pythonImportsCheck = [ "scrape_harness" ];
        };

      treefmtEval = forAll (pkgs: treefmt-nix.lib.evalModule pkgs ./treefmt.nix);
    in
    {
      # For a CONSUMER flake (e.g. listing-insights): the harness as a Python library, its dependency
      # set, the pinned browser, the CLI wrapper's environment, and the consumer tools (env-doctor,
      # leak-gate, ship) built from the consumer's own pipeline.toml [consumer] table.
      lib = {
        cliSmoke = cliSmokeFor;
        pyDeps = pyDepsFor;
        python = pythonFor;
        browsers = browsersFor;
        filterLists = filterListsFor; # the pinned EasyList + EasyPrivacy (a consumer's tests set the env from it)
        wrapperArgs = wrapperArgsFor;
        sshKeyTools = sshKeyToolsFor;
        # args: { pkgs, consumer, extraCatalogue ? { }, plugin ? "./plugin", shipLocalChecks ? [ ] } (see nix/consumer.nix)
        consumerTools = args: import ./nix/consumer.nix ({ inherit lib; } // args);
      };

      # `nix flake init -t <scrape-harness>#default`: a consumer skeleton (flake via lib, a pipeline.toml whose
      # FILL-ME values refuse to load, a Source stub, its schemas, tests, the shared CI include).
      templates.default = {
        path = ./templates/default;
        description = "a scrape-harness consumer: fill every FILL-ME in pipeline.toml, then `<cli> claude-settings --write`";
      };

      formatter = forAll (pkgs: treefmtEval.${pkgs.stdenv.hostPlatform.system}.config.build.wrapper);

      packages = forAll (pkgs: {
        default = li pkgs;

        inherit (consumerTools pkgs)
          env-doctor
          leak-gate
          public-gate
          ship
          ;
        # The plugin as a store path: a consumer's tests run its guard against their pipeline.toml.
        # The plugin at the pinned marketplace rev, as a store path (hooks run from it).
        plugin = pkgs.runCommandLocal "scrape-harness-plugin" { } ''
          cp -r ${kattakath-skills}/plugins/scrape-harness "$out"
        '';
      });

      apps = forAll (
        pkgs:
        lib.genAttrs [ "env-doctor" "leak-gate" "public-gate" "ship" ] (n: {
          type = "app";
          program = lib.getExe self.packages.${pkgs.stdenv.hostPlatform.system}.${n};
          meta.description = "scrape-harness ${n}";
        })
        // {
          default = {
            type = "app";
            program = lib.getExe self.packages.${pkgs.stdenv.hostPlatform.system}.default;
            meta.description = "the ${consumer.cli} CLI";
          };
        }
      );

      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = devPackagesFor pkgs;
          PLAYWRIGHT_BROWSERS_PATH = browsersFor pkgs;
          PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "true";
          SCRAPE_HARNESS_FILTER_LISTS = filterListsFor pkgs;
          SCRAPE_HARNESS_FILTER_LISTS_PIN = filterListsRev;
          # the plugin at the pinned marketplace rev: tests and `--plugin-dir` use it
          SCRAPE_HARNESS_PLUGIN = "${self.packages.${pkgs.stdenv.hostPlatform.system}.plugin}";
          shellHook = ''
            export PRJ_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
            [ -n "''${${consumer.data_env}:-}" ] || echo "${consumer.cli}: ${consumer.data_env} is unset (the CLI refuses to run)" >&2
          '';
        };
      });

      checks = forAll (
        pkgs:
        let
          system = pkgs.stdenv.hostPlatform.system;
        in
        self.packages.${system}
        // {
          formatting = treefmtEval.${system}.config.build.check self;

          cli-smoke = cliSmokeFor {
            inherit pkgs consumer;
            package = self.packages.${system}.default;
            commands = [
              "crawl-lists"
              "fetch-ads"
              "ab"
            ];
            pipeline = "${self}/pipeline.toml";
          };

          # GOES RED WHEN: the template's record schemas drift from the example consumer's (one source).
          template-schemas = pkgs.runCommandLocal "template-schemas" { } ''
            for s in observation listing; do
              cmp ${./examples/example_site/schemas}/$s.v1.schema.json ${./templates/default/schemas}/$s.v1.schema.json
            done
            touch "$out"
          '';

          # GOES RED WHEN: the UNFILLED template loads: its FILL-ME values must refuse, naming each hole.
          template-unfilled-refuses =
            pkgs.runCommandLocal "template-unfilled-refuses"
              { nativeBuildInputs = [ (pkgs.python3.withPackages (ps: [ (pythonFor pkgs ps) ])) ]; }
              ''
                export HOME="$TMPDIR" SCRAPE_HARNESS_PIPELINE=${./templates/default/pipeline.toml}
                if python -m scrape_harness.cli plugins >out 2>err; then echo "the unfilled template loaded" >&2; exit 1; fi
                # one test per line: `set -e` ignores every command of an && list but the last
                for want in "still the starter template" "consumer.cli," "consumer.approval_key_fingerprint" "site_hosts[0]"; do
                  grep -qF "$want" err || { echo "the refusal does not name: $want" >&2; cat err >&2; exit 1; }
                done
                touch "$out"
              '';

          # GOES RED WHEN: the template, filled with synthetic values, does not build and pass its own tests
          # (pipeline loads, source declared, generated settings current), the way a new consumer would.
          template-instantiated =
            let
              ps = pkgs.python3Packages;
              harnessPy = pkgs.python3.withPackages (p: [ (pythonFor pkgs p) ]);
              filled = pkgs.runCommandLocal "template-filled" { nativeBuildInputs = [ harnessPy ]; } ''
                cp -r ${./templates/default} "$out"; chmod -R u+w "$out"; cd "$out"
                sed -i \
                  -e 's/^cli = "FILL-ME"/cli = "tplsite"/' -e 's/^env_prefix = "FILL-ME"/env_prefix = "TPL"/' \
                  -e 's/^data_env = "FILL-ME"/data_env = "TPL_DATA"/' -e 's/^state_env = "FILL-ME"/state_env = "TPL_STATE"/' \
                  -e 's/^state_dirname = "FILL-ME"/state_dirname = "tplsite"/' \
                  -e 's/^default_site = "FILL-ME"/default_site = "template.invalid"/' \
                  -e 's/^default_region = "FILL-ME"/default_region = "region-one"/' \
                  -e 's/^approval_key_fingerprint = "FILL-ME"/approval_key_fingerprint = "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"/' \
                  -e 's/^site_hosts = \["FILL-ME"\]/site_hosts = ["template.invalid"]/' \
                  -e 's/^FILL-ME = "site_source.source:Site"/"template.invalid" = "site_source.source:Site"/' pipeline.toml
                sed -i 's/    site = "FILL-ME"/    site = "template.invalid"/' src/site_source/source.py
                sed -i 's/^FILL-ME = "scrape_harness.cli:main"/tplsite = "scrape_harness.cli:main"/' pyproject.toml
                if grep -rnE '"FILL-ME"|^FILL-ME ' pipeline.toml src pyproject.toml; then echo "FILL-ME left after filling" >&2; exit 1; fi
                HOME="$TMPDIR" SCRAPE_HARNESS_PIPELINE="$out/pipeline.toml" python -m scrape_harness.cli claude-settings --write
              '';
            in
            ps.buildPythonPackage {
              pname = "template-instantiated";
              version = "0";
              pyproject = true;
              src = filled;
              build-system = [ ps.hatchling ];
              dependencies = [ (pythonFor pkgs ps) ];
              nativeCheckInputs = [ ps.pytestCheckHook ];
              preCheck = "export HOME=$TMPDIR";
            };

          # GOES RED WHEN: a failing shipLocalChecks command does not refuse the push (exit 3),
          # a later command still runs after it, a passing list fails, the check is not printed, or an
          # empty list prints anything (other consumers must be unchanged).
          ship-local-checks =
            let
              tools =
                cs:
                (self.lib.consumerTools {
                  inherit pkgs consumer;
                  shipLocalChecks = cs;
                }).ship-local-checks;
              pass = tools [
                "true"
                "echo marker-ok"
              ];
              fail = tools [
                "true"
                "exit 7"
                "touch should-not-run"
              ];
              none = tools [ ];
            in
            pkgs.runCommandLocal "ship-local-checks" { } ''
              cd "$TMPDIR"
              got="$(${pass}/bin/ship-local-checks)" || { echo "a passing list failed" >&2; exit 1; }
              printf '%s\n' "$got" | grep -qx '== local check: echo marker-ok' || { echo "not printed: $got" >&2; exit 1; }
              printf '%s\n' "$got" | grep -qx 'marker-ok' || { echo "the command did not run" >&2; exit 1; }
              rc=0; ${fail}/bin/ship-local-checks >log 2>err || rc=$?
              [ "$rc" = 3 ] || { echo "a failing check exited $rc, not 3" >&2; exit 1; }
              grep -q 'refused, a local check failed (exit 7): exit 7' err || { cat err >&2; exit 1; }
              [ ! -e should-not-run ] || { echo "a check after the failure still ran" >&2; exit 1; }
              [ -z "$(${none}/bin/ship-local-checks)" ] || { echo "an empty list printed output" >&2; exit 1; }
              touch "$out"
            '';

          # GOES RED WHEN: the leak gate's persona.allow handling is wrong: an allowed form must pass, while
          # the forbidden name alone, a line mixing it with an allowed form, and (unset) the allowed form
          # itself must all fail. Synthetic strings in a throwaway repo; the real lists stay outside.
          leak-gate-allow =
            pkgs.runCommandLocal "leak-gate-allow"
              {
                nativeBuildInputs = [
                  pkgs.git
                  (consumerTools pkgs).leak-gate
                ];
              }
              ''
                export HOME="$TMPDIR"; cd "$TMPDIR"
                printf 'acmeperson\n' > lits; export ${consumer.env_prefix}_FORBIDDEN_LITERALS="$TMPDIR/lits"
                gate() { # $1 = expected rc (0 pass, 1 refuse); $2 = file content; the repo is fresh each time
                  rm -rf r; git init -q r; cd r
                  git config user.name tester; git config user.email t@example.invalid
                  [ -n "''${ALLOW:-}" ] && git config persona.allow "$ALLOW"
                  printf '%s\n' "$2" > f; git add f; git commit -q -m "synthetic"
                  rc=0; leak-gate >/dev/null 2>&1 || rc=$?
                  cd ..
                  [ "$rc" = "$1" ] || { echo "expected rc $1, got $rc for: $2 (allow=''${ALLOW:-unset})" >&2; exit 1; }
                }
                ALLOW='acmeperson/skills|"acmeperson": \{'
                gate 0 'see acmeperson/skills for the plugin'
                gate 0 '"acmeperson": {'
                gate 1 'by Acmeperson'
                gate 1 'acmeperson/skills and acmeperson.example'
                ALLOW=""
                gate 1 'see acmeperson/skills for the plugin'
                touch "$out"
              '';

          # GOES RED WHEN: public-gate lets a forbidden literal through anywhere a public push would carry it
          # (any file incl. flake.lock, the commit message or author), lets a compressed file through, cuts out
          # more than the exact allowed string, or runs without a literal list. Synthetic strings only.
          public-gate =
            pkgs.runCommandLocal "public-gate"
              {
                nativeBuildInputs = [
                  pkgs.git
                  (consumerTools pkgs).public-gate
                ];
              }
              ''
                export HOME="$TMPDIR"; cd "$TMPDIR"
                printf 'acmeperson\nacme town\n' > lits; printf 'acme town-hints@6\n' > allow
                export ${consumer.env_prefix}_PUBLIC_LITERALS="$TMPDIR/lits"
                gate() { # $1 expected rc; $2 file name; $3 content; $4 commit message
                  rm -rf r; git init -q r; cd r
                  git config user.name tester; git config user.email t@example.invalid
                  printf '%s\n' "$3" > "$2"; git add -A; git commit -q -m "$4"
                  rc=0; public-gate >/dev/null 2>&1 || rc=$?
                  cd ..
                  [ "$rc" = "$1" ] || { echo "expected rc $1, got $rc for $2: $3 / $4" >&2; exit 1; }
                }
                gate 0 f 'nothing to see' 'clean'
                gate 1 f 'by Acmeperson' 'clean'
                gate 1 flake.lock '"owner": "acmeperson"' 'clean'
                gate 1 f 'nothing to see' 'thanks acmeperson'
                gate 1 f.gz 'nothing to see' 'clean'
                gate 1 f 'method acme town-hints@6' 'clean'
                export ${consumer.env_prefix}_PUBLIC_ALLOW="$TMPDIR/allow"
                gate 0 f 'method acme town-hints@6' 'clean'
                gate 1 f 'acme town-hints@6 near acme town' 'clean'
                unset ${consumer.env_prefix}_PUBLIC_LITERALS
                gate 2 f 'nothing to see' 'clean'
                touch "$out"
              '';

          # GOES RED WHEN: the consumer-facing lib cannot be called the way a consumer flake calls it
          # (every argument, incl. plugin and extraCatalogue). The harness's own tools pass fewer.
          consumer-lib =
            (self.lib.consumerTools {
              inherit pkgs consumer;
              plugin = "${self.packages.${system}.plugin}";
              extraCatalogue.SYNTHETIC_KEY = {
                need = "optional";
                secret = true;
                note = "synthetic";
              };
            }).ship;

          # GOES RED WHEN: ruff finds a lint error anywhere in the Python sources or hooks.
          ruff = pkgs.runCommandLocal "ruff" { nativeBuildInputs = [ pkgs.ruff ]; } ''
            cd ${src} && ruff check --no-cache . && touch "$out"
          '';

          # GOES RED WHEN: a schema is not valid 2020-12, an example stops validating, or an
          # INVALID example starts validating (the gate that proves the gate can fail).
          schemas =
            pkgs.runCommandLocal "schemas"
              {
                nativeBuildInputs = [ pkgs.check-jsonschema ];
              }
              ''
                cd ${./schemas}
                check-jsonschema --check-metaschema ./*.schema.json
                # every example against its schema, whatever its version (<name>.v<N>.schema.json)
                schema_of() { n="''${1##*/}"; n="''${n%%.*}"; set -- "$n".v*.schema.json; [ -f "$1" ] || { echo "no schema for $n" >&2; exit 1; }; echo "$1"; }
                for ex in examples/*.example.json; do
                  check-jsonschema --schemafile "$(schema_of "$ex")" "$ex"
                done
                for bad in examples/invalid/*.json; do
                  if check-jsonschema --schemafile "$(schema_of "$bad")" "$bad" >/dev/null 2>&1; then
                    echo "INVALID example passed: $bad" >&2; exit 1
                  fi
                done
                # The example consumer's record schemas: same three gates.
                cd ${./examples/example_site/schemas}
                check-jsonschema --check-metaschema ./*.schema.json
                for s in observation listing; do
                  check-jsonschema --schemafile "$s.v1.schema.json" "examples/$s.example.json"
                done
                for bad in examples/invalid/*.json; do
                  s="''${bad##*/}"; s="''${s%%.*}"
                  if check-jsonschema --schemafile "$s.v1.schema.json" "$bad" >/dev/null 2>&1; then
                    echo "INVALID example passed: $bad" >&2; exit 1
                  fi
                done
                touch "$out"
              '';

          # GOES RED WHEN: a binary the dev shell promises does not resolve from devPackagesFor.
          toolchain-complete =
            pkgs.runCommandLocal "toolchain-complete" { nativeBuildInputs = devPackagesFor pkgs; }
              ''
                export PRJ_ROOT=/nonexistent
                for bin in ${lib.escapeShellArgs requiredBins}; do
                  command -v "$bin" >/dev/null || { echo "dev shell is missing: $bin" >&2; exit 1; }
                done
                touch "$out"
              '';
        }
      );
    };
}
