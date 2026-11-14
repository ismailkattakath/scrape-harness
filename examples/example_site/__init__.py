"""The EXAMPLE consumer: a synthetic classifieds site on the reserved `example.invalid` domain (RFC 2606),
so it can never resolve. It exists to drive the harness end to end and to show a consumer's shape:
a Source (source.py), its record schemas (schemas/), its fixtures (tests/fixtures/) and its pipeline.toml
(the repo root's). Its markup is invented; it imitates no real site."""
