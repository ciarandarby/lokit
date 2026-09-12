# Contributing to Lokit

Bug reports, fixes, documentation improvements, and feature proposals are welcome.
Use the [issue forms](https://github.com/ciarandarby/lokit/issues/new/choose) to
report a reproducible problem or describe a feature. For a substantial API or
architecture change, start a discussion in an issue before implementing it.

This repository contains the Python package, its Rust and .NET runtime code,
and the tests and tools used to build PyPI releases. Examples, benchmarks, and
editor extensions are maintained separately.

## Getting started

Fork the repository, clone your fork, and create a branch for your change.
Use Python 3.10 or newer, uv, and a Rust toolchain compatible with the crate
manifests. The [CI workflow](workflows/ci.yml) records the toolchains tested by
the project.

From the repository root, install the locked development dependencies and
the package:

```sh
uv sync --frozen --all-groups
```

The editable install picks up Python source changes. After modifying Rust
source, rebuild the extension:

```sh
uv sync --frozen --all-groups --reinstall-package lokit-python
```

## Making changes

- Keep each pull request focused on one problem and describe the resulting behavior.
- All Python code must be type-safe and pass `mypy --strict`. Preserve the stricter
  settings in `pyproject.toml`; do not weaken checks to silence errors.
- Add or update regression tests when changing behavior. Documentation-only
  changes do not need new tests.
- For parser and exporter changes, consider round trips, streaming behavior,
  malformed input, and preservation of tags and metadata.
- Add small, redistributable fixtures under `tests/fixtures/` when needed,
  including attribution and license information for third-party samples.
- Keep generated binaries, build outputs, virtual environments, and private
  sample documents out of pull requests.
- Explain compatibility changes. Version bumps and publishing are handled by
  the maintainer as part of a release.

## Python checks

Run the checks relevant to your change and report their results in the PR:

```sh
uv run --no-sync pytest
uv run --no-sync mypy -p lokit
uv run --no-sync mypy -p lokit_office_runtime
uv run --no-sync mypy --strict setup.py tools tests
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync python tools/verify_release_versions.py
uv run --no-sync python tools/verify_native_install.py
```

For a focused test run, pass a test file to pytest, such as
`uv run --no-sync pytest tests/test_public_api.py`.

Database integration tests use `LOKIT_TEST_PG_URI`. Point it at a disposable
PostgreSQL database: the fixtures drop and recreate tables. These tests skip
when the variable is unset. Mention skipped or unavailable checks in your PR.

## Rust and Office changes

For each Rust crate you change, run formatting, linting, and tests. For example:

```sh
cargo fmt --manifest-path native/interchange/Cargo.toml -- --check
cargo clippy --locked --manifest-path native/interchange/Cargo.toml --all-targets --all-features -- -D warnings
cargo test --locked --manifest-path native/interchange/Cargo.toml --all-features
```

Use `native/lokit-format/Cargo.toml` for the shared format crate. Changes there
also need validation through the Python extension that consumes it.

Office worker changes require the .NET SDK selected by `global.json`:

```sh
dotnet restore src/office/Lokit.Office.sln --locked-mode
dotnet build src/office/Lokit.Office.sln --configuration Release --no-restore
dotnet test src/office/Lokit.Office.sln --configuration Release --no-build --no-restore
```

Some Python Office protocol tests also require a Debug worker build. The
platform-specific runtime build and staging steps are in
[CI](workflows/ci.yml) and the [release workflow](workflows/publish.yml).

Build and packaging changes should pass the wheel and source-distribution
smoke checks defined in those workflows. CI covers Python 3.10–3.14 and the
supported release platforms.

## Opening a pull request

Use the PR template to explain the problem, the change, and how you verified it.
Link a related issue when one exists; an issue is not required for a small fix.
Draft PRs are welcome for work in progress. Keep review discussion focused on
the code and follow the [code of conduct](CODE_OF_CONDUCT.md).

[@ciarandarby](https://github.com/ciarandarby) maintains the project and owns
all paths in [CODEOWNERS](CODEOWNERS). The project uses the [MIT license](../LICENSE).
