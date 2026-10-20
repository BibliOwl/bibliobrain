# Contributing to Bibliobrain

Thank you for helping improve Bibliobrain. Bug reports, feature discussions, documentation
feedback, and code contributions are welcome.

## Before opening a pull request

For a substantial change, open an issue first so the intended behavior and scope can be agreed
before implementation. Keep pull requests focused and do not mix unrelated refactors with the
proposed change.

Never submit credentials, private documents, personal data, model files without redistribution
rights, or third-party code whose license is unknown or incompatible with this project.

## Development checks

The HTTP entry point is `app_api.py`. It assembles the feature routers in `src/api/`
(chat, library, notebooks, discovery, drafting, graphs, settings, models, and system
routes). `src/api/application.py` owns application startup, middleware and shared
server state. Routers may use that shared state; the application module must not
import the routers.

The browser loads `static/js/app.js` for shared state and the scripts in
`static/js/app/` for individual features. Their explicit order in `static/index.html`
is part of startup: these are classic scripts with a shared global scope. Load every
feature before `settings.js` initializes the interface and before
`static-event-bindings.js` connects its controls. No bundler or Node.js runtime is
needed by the application. UI source tests use `read_app_javascript()` from
`tests/conftest.py` to read these scripts in browser order.

Use Python 3.14 and the locked dependencies described in the README. Before submitting a pull
request, run:

```powershell
python -m ruff check .
python -m mypy
python -m pytest -q
python -m pip check
```

Add or update tests for every behavioral change. If a dependency or vendored component changes,
update `requirements.lock`, `THIRD_PARTY_NOTICES.md`, and the relevant license files.

## Contributor License Agreement

Bibliobrain is distributed publicly under AGPL-3.0-only and may also be offered under separate
commercial terms. To preserve that dual-licensing option, every non-trivial contribution that
is merged into the project requires acceptance of [CLA.md](CLA.md).

The CLA is non-exclusive: contributors retain ownership of their contributions. It grants the
project owner the rights needed to maintain, distribute, sublicense, and license the contribution
under open-source or commercial terms.

Acceptance is private: your legal name is never published. To accept the CLA as an
individual, send the following statement by email to bibliobrain@proton.me, replacing the
bracketed values:

```text
I have read and accept CLA.md version 1.1 as [full legal name], GitHub account [account], on [YYYY-MM-DD].
```

Then write "CLA 1.1 accepted privately" in the pull-request description. A maintainer confirms
receipt before merging. The email is kept confidential and used only to administer the rights
in Bibliobrain: see [PRIVACY.md](PRIVACY.md). A contributor acting for an employer or another
legal entity must have authority to contribute and may be asked for a separate corporate
agreement.

Pull requests that do not require copyright permission, such as reports containing no original
code or prose, do not require CLA acceptance. The maintainers decide whether a contribution is
non-trivial for this purpose.

## Review and licensing

Submission does not guarantee acceptance. Contributors must identify third-party material and
all known license, patent, or employer restrictions affecting their contribution. Unless clearly
marked otherwise before submission, discussion text remains discussion and is not incorporated
as a contribution.
