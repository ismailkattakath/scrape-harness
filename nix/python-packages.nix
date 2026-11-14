# Python packages nixpkgs does not carry: crawlee and the two it hard-depends on. Exact pins;
# every hash is the PyPI artifact sha256 (cross-checked against the PyPI JSON API on 2026-11-07).
# crawlee and proclimits are pure-Python py3-none-any wheels (crawlee's sdist needs uv_build, which
# would add a build backend for no behaviour); impit is built from source.
#
# Operator decision 2026-11-07: crawlee WITH impit, impit IN USE (docs/design.md ADR-0003).
{ pkgs, ps }:
let
  inherit (pkgs) lib;
in
rec {
  proclimits = ps.buildPythonPackage rec {
    pname = "proclimits";
    version = "0.2.0";
    format = "wheel";
    src = ps.fetchPypi {
      inherit pname version format;
      dist = "py3";
      python = "py3";
      hash = "sha256-P9Ns4xtdctm0sG3HiMfDJXr+w09SpN7FqmmxBdfNzac=";
    };
    pythonImportsCheck = [ "proclimits" ];
  };

  # Rust (maturin + pyo3). It vendors apify's forks of rustls/h2/hyper-util/tower-http by git rev;
  # fetchCargoVendor pins all of them by the one hash below. aws-lc-sys needs cmake.
  impit = ps.buildPythonPackage rec {
    pname = "impit";
    version = "0.15.1";
    pyproject = true;
    src = ps.fetchPypi {
      inherit pname version;
      hash = "sha256-KTcWZt2KRfGOLqzIgl8UKvHswy/jfcdGRYbPKh+yPIU=";
    };
    cargoDeps = pkgs.rustPlatform.fetchCargoVendor {
      inherit pname version src;
      hash = "sha256-RDl0b65B8xyrbOOC7jvWNjJ+IP79HmHP7dKbQkFh7vI=";
    };
    nativeBuildInputs = [
      pkgs.rustPlatform.cargoSetupHook
      pkgs.rustPlatform.maturinBuildHook
      pkgs.cmake
      pkgs.perl
    ];
    dontUseCmakeConfigure = true;
    # Upstream's own flag (the sdist's .cargo/config.toml `build.rustflags`), which the RUSTFLAGS the
    # Nix hooks export would otherwise override: reqwest's http3 feature refuses to compile without it.
    env.RUSTFLAGS = "--cfg reqwest_unstable";
    pythonImportsCheck = [ "impit" ];
  };

  crawlee = ps.buildPythonPackage rec {
    pname = "crawlee";
    version = "1.11.1";
    format = "wheel";
    src = ps.fetchPypi {
      inherit pname version format;
      dist = "py3";
      python = "py3";
      hash = "sha256-El7Ul+Rgfl5Ywi5YwInl1b7cO3OS1gGSO4O/4MpyK3k=";
    };
    dependencies =
      (with ps; [
        async-timeout
        cachetools
        colorama
        more-itertools
        protego
        psutil
        pydantic
        pydantic-settings
        typing-extensions
        yarl
        # extras used: [playwright] (PlaywrightCrawler + fingerprint generator), [parsel]
        playwright
        browserforge
        apify-fingerprint-datapoints
        parsel
      ])
      ++ [
        impit
        proclimits
      ];
    pythonImportsCheck = [
      "crawlee.crawlers"
      "crawlee.http_clients"
    ];
  };
}
