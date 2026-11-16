# Local verification and release checklist

This document records v0.1 release verification using synthetic data and
temporary data homes. No real user archive is required for these checks.

## Environments

Locally exercised on macOS with Python 3.14.6, SQLite 3.53.4, and Node.js 26.7.0.
The GitHub Actions matrix covers Linux/Python 3.11 and 3.14,
macOS/Python 3.13, and Windows/Python 3.13, plus renderer and distribution jobs.
Actual results for each published revision are available in
[Checks](https://github.com/jovequli/agent-library/actions/workflows/ci.yml).

## Checks performed locally

- Editable installation into the source checkout's ignored `.venv` succeeded.
- A source distribution was built using the declared setuptools backend, and
  the actual archive passed completeness, vendor integrity, and source boundary
  checks.
- A real wheel was built outside the source tree. Its contents passed the source
  boundary check and included all offline assets, all 60 KaTeX fonts, and full
  third-party license texts.
- The wheel was installed with `--no-deps` into a second temporary virtual
  environment. With `PYTHONPATH` unset and the working directory outside the
  source checkout, its actual installed module and CLI were exercised.
- Installed CLI checks covered version, a fictional mixed-provider demo, search,
  doctor, backup, and restoration into a different data home. The demo had six
  sessions and 70 normalized message blocks.
- A selected conversation was exported to a context ZIP, imported into a new
  collection, and backed up successfully from the installed wheel.
- The installed HTTP application served its reader, API state, a packaged font,
  and a packaged third-party license successfully from loopback. That test
  server was stopped after verification.
- The local release regression set contains 102 Python tests, plus 54 JavaScript
  tests: 16 Markdown, 20 mathematics, and 18 interface workflow checks. Fixtures
  are synthetic. These suites include importer variants, standalone JSON input,
  library isolation, incremental updates, storage/backup workflows, handoff
  round trips, real HTTP boundaries, lossless legacy migration and separate task galleries. All 156 checks passed locally without
  warnings. Rerun the set after source changes.
- Actual browser QA using only the fictional demo verified opening at the
  latest message, stable upward history scrolling, preview without moving the
  conversation, explicit message navigation, saved handoff notes, and backup/
  restore into an independent library. The preview server was stopped afterward.
  The bilingual screenshots in `docs/screenshots/` show only fictional demo
  sessions, with no private conversation or personal path. The default
  `README.md` is Simplified Chinese, `README.en.md` is English, and
  `README.zh-CN.md` is a compatibility link to the default page.
- The macOS source launcher passed shell syntax checking. The Windows launcher
  is provided and reviewed but has not been run on Windows in this session.

These checks apply to the source snapshot used for each build. Rebuild and
recheck final distribution files after further code changes.

## Reproduce

Use an activated project virtual environment and an output directory outside
the repository. No upload or publishing command is part of this workflow.

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
python scripts/verify_vendor.py
python scripts/check_release.py
npm ci
npm test
```

Build the source archive first, then build the wheel from that archive. This
checks that the source archive is sufficient without the checkout. Install only
the declared build backend in the development environment; it is not a runtime
dependency. The commands below use a macOS/Linux temporary output path; choose
an equivalent outside-project directory on Windows.

```sh
python -m pip install 'setuptools>=77'
python -c "from pathlib import Path; Path('/tmp/agent-library-dist').mkdir(exist_ok=True)"
python -c "import setuptools.build_meta as b; b.build_sdist('/tmp/agent-library-dist')"
python scripts/check_distribution.py /tmp/agent-library-dist/agent_history_library-0.1.0.tar.gz
python scripts/check_release.py /tmp/agent-library-dist/agent_history_library-0.1.0.tar.gz
python -m pip wheel --no-deps --wheel-dir /tmp/agent-library-dist /tmp/agent-library-dist/agent_history_library-0.1.0.tar.gz
python scripts/check_distribution.py /tmp/agent-library-dist/agent_history_library-0.1.0-py3-none-any.whl
python scripts/check_release.py /tmp/agent-library-dist/agent_history_library-0.1.0-py3-none-any.whl
```

`check_distribution.py` uses only the standard library. It checks the actual
wheel or source archive without extracting it: required modules and documents,
all vendored file hashes and licenses, font completeness, wheel RECORD hashes,
and the same source-only boundary checks used for the project directory.

Before public release, additionally review the final artifact and complete Git
history for private content, decide the final project license/identity, set a
private vulnerability reporting channel, and run the configured target-platform
jobs. Source boundary heuristics do not identify every possible private string.
Publishing is a separate action and is not enabled by these checks.
