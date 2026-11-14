# Read by both `formatter` (nix fmt) and the `formatting` check, so they cannot disagree.
{
  projectRootFile = "flake.nix";
  programs.nixfmt.enable = true;
  programs.ruff-format.enable = true;
  settings.global.excludes = [
    "flake.lock"
    "result"
    "result-*"
    "tests/fixtures/*"
    ".claude/statusline.py" # generated from src/scrape_harness/status.py; a drift test compares it byte for byte
  ];
}
