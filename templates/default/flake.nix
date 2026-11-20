# A scrape-harness CONSUMER, from `nix flake init -t <scrape-harness>#default`. The generic core (CLI,
# crawl, approvals, store, the Claude Code plugin) is the scrape-harness input; this repo holds only its
# Source, record schemas and pipeline.toml (fill every FILL-ME there first).
#
#   nix develop          → python + the harness, this project's CLI ([consumer].cli), ruff, gitleaks
#   nix run              → the CLI;  nix run .#ship → THE push;  nix run .#env-doctor / .#leak-gate
#   nix run .#<cli> -- … → the CLI with this checkout's .env* and .envrc.local loaded, from any shell
#                          (also .#preflight, .#cli-spec)
#   nix flake check      → the package build (runs pytest) + formatting
{
  description = "a scrape-harness pipeline";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    scrape-harness = {
      url = "github:ismailkattakath/scrape-harness";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      scrape-harness,
    }:
    let
      inherit (nixpkgs) lib;
      harness = scrape-harness.lib;
      systems = [
        "aarch64-darwin"
        "aarch64-linux"
        "x86_64-linux"
      ];
      forAll = f: lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
      pyproject = lib.importTOML ./pyproject.toml;
      consumer = (lib.importTOML ./pipeline.toml).consumer;
      plugin = pkgs: scrape-harness.packages.${pkgs.stdenv.hostPlatform.system}.plugin;
      pyDepsFor = pkgs: ps: [ (harness.python pkgs ps) ] ++ harness.pyDeps pkgs ps;
      src = lib.fileset.toSource {
        root = ./.;
        fileset = lib.fileset.unions [
          ./pyproject.toml
          ./pipeline.toml
          ./src
          ./schemas
          ./tests
          ./.claude
        ];
      };

      cli =
        pkgs:
        let
          ps = pkgs.python3Packages;
        in
        ps.buildPythonApplication {
          pname = pyproject.project.name;
          inherit (pyproject.project) version;
          pyproject = true;
          inherit src;
          build-system = [ ps.hatchling ];
          dependencies = pyDepsFor pkgs ps;
          nativeCheckInputs = [ ps.pytestCheckHook ];
          makeWrapperArgs = harness.wrapperArgs {
            inherit pkgs consumer;
            rev = self.shortRev or self.dirtyShortRev or "unknown";
            pipeline = "${src}/pipeline.toml";
          };
          meta.mainProgram = consumer.cli;
        };
      tools =
        pkgs:
        harness.consumerTools {
          inherit pkgs consumer;
          plugin = "${plugin pkgs}";
          cliPackage = cli pkgs; # apps.<cli>, apps.preflight, apps.cli-spec
          # cliSpecDoc = ./docs/cli.md;  # commit `<cli> cli spec --format md > docs/cli.md`, then this adds checks.cli-spec-drift
        };
    in
    {
      packages = forAll (
        pkgs:
        {
          default = cli pkgs;
          inherit (tools pkgs) env-doctor leak-gate ship;
        }
        // (tools pkgs).appPackages
      );
      apps = forAll (
        pkgs:
        lib.genAttrs [ "env-doctor" "leak-gate" "ship" ] (n: {
          type = "app";
          program = lib.getExe self.packages.${pkgs.stdenv.hostPlatform.system}.${n};
        })
        // (tools pkgs).apps
      );
      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (
              ps:
              pyDepsFor pkgs ps
              ++ [
                ps.pytest
                (ps.mkPythonEditablePackage {
                  pname = pyproject.project.name;
                  inherit (pyproject.project) version scripts;
                  root = "$PRJ_ROOT/src";
                  dependencies = pyDepsFor pkgs ps;
                })
              ]
            ))
            pkgs.ruff
            pkgs.gitleaks
            pkgs.git
            (harness.sshKeyTools pkgs)
          ];
          PLAYWRIGHT_BROWSERS_PATH = harness.browsers pkgs;
          PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "true";
          SCRAPE_HARNESS_PLUGIN = "${plugin pkgs}";
          shellHook = ''export PRJ_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"'';
        };
      });
      checks = forAll (pkgs: self.packages.${pkgs.stdenv.hostPlatform.system} // (tools pkgs).checks);
    };
}
