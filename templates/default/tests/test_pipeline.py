"""The project's declaration loads, its source is declared and the generated settings are current."""

from conftest import ROOT

from scrape_harness import claude_settings, interfaces, pipeline


def test_the_pipeline_loads_and_declares_the_source():
    pl = pipeline.current()
    assert pl.stage_names == ["crawl-lists", "fetch-ads", "reparse"]
    src = interfaces.load("sources", pl.consumer.default_site)
    assert src.site == pl.consumer.default_site


def test_the_generated_settings_are_current():
    """Fails until `<cli> claude-settings --write` has been run (and after any pipeline.toml change)."""
    assert claude_settings.check(ROOT), "run `<cli> claude-settings --write` and commit the result"
