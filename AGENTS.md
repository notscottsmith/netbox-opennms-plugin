# Agent guide — netbox-opennms-plugin

NetBox plugin that renders monitoring **intent** (Requisitions = a live NetBox
filter over Devices/VMs) into OpenNMS requisitions and imports them over the
REST provisioning API. Sync runs as NetBox background Jobs; every sync is
idempotent render-and-replace.

## Commands (everything runs in Docker — no host Python/venv needed)

- `make verify` — ruff + full unit suite in a throwaway NetBox stack (the CI gate)
- `make test` / `make lint` — the two halves individually
- `make build` — wheel + sdist into `dist/` (pinned python image)
- `make integration` — live OpenNMS Horizon 36 round-trip (slow; boots OpenNMS)
- `make makemigrations` — generate plugin migrations + assert none missing
- Single test: `docker compose -f compose.yml run --rm netbox \
  '/opt/netbox/venv/bin/python manage.py test netbox_opennms.tests.<module>.<TestCase> -v2'`
  (then `docker compose -f compose.yml down -v`)

## Gotchas

- Version is single-sourced in `netbox_opennms/__init__.py` (`__version__`);
  `pyproject.toml` reads it via AST — never bump it anywhere else.
- A pre-commit hook (`scripts/bump_patch_version.py`, wired via
  `.pre-commit-config.yaml`) auto-bumps the patch component of `__version__`
  (and the README install pin) on every commit — it's not a release, just a
  running counter (see RELEASING.md). It backs off whenever
  `netbox_opennms/__init__.py` is already staged, so a manual
  `chore(release)` bump is never clobbered. Run `pre-commit install` once
  per clone to activate it.
- `.github/workflows/release.yml` and the `pypi` environment are bound to PyPI
  Trusted Publishing — renaming either breaks OIDC (see RELEASING.md).
- Query-count regression tests compare against
  `netbox_opennms/tests/query_counts.json`; after intentional query changes run
  `make regen-counts`.
- Ruff excludes `*/migrations/*`; everything else must pass `E,F,I,UP,B`.
- Every new/edited source file needs the SPDX header
  (`SPDX-License-Identifier: MIT`).

## Commits

Conventional Commits; `git commit -s` (DCO, human identity) plus an
`Assisted-by: <Agent>:<model>` trailer on AI-assisted commits. PRs start from
an issue and are squash-merged. See CONTRIBUTING.md.

## Agent skills

### Issue tracker

Issues live in GitHub Issues on `notscottsmith/netbox-opennms-plugin`, via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Domain docs

Single-context: `CONTEXT.md` + `docs/adr/` at the repo root (created lazily, not yet present). See `docs/agents/domain.md`.
