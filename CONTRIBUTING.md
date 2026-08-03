# Contributing

This project uses Git Flow, Semantic Versioning, and unscoped Conventional
Commits. The production branch is named `main`.

## Branches

- `main` contains released code only. Every release commit on `main` has an
  annotated `vX.Y.Z` tag.
- `develop` integrates work for the next release.
- `feature/*` branches start from `develop` and merge back into `develop`.
- `bugfix/*` branches start from `develop` and merge back into `develop`. Use
  them for defects that do not require an immediate production release.
- `release/X.Y.Z` branches start from `develop`. Limit them to release
  stabilization and version or documentation updates, then merge them into
  both `main` and `develop`. Tag the resulting release commit on `main` as
  `vX.Y.Z`.
- `hotfix/X.Y.Z` branches start from the latest released commit on `main`. Use
  them for urgent defects in a published release, then merge them into both
  `main` and `develop`. Tag the resulting patch release commit on `main` as
  `vX.Y.Z`.

Use non-fast-forward merges for `release/*` and `hotfix/*` so the release
topology remains visible. Delete short-lived branches only after their merged
commits and tags are available on the remote.

## Commits

Write commit subjects as:

```text
type: imperative summary
```

Do not add a parenthesized scope. Use a breaking-change footer when needed.
Common types are `feat`, `fix`, `refactor`, `test`, `docs`, `build`, `ci`,
`chore`, `perf`, and `revert`.

Examples:

```text
fix: close changed-scope audit gaps
feat: detect bounded stream consumption
docs: explain hotfix release flow
```

Both `bugfix/*` and `hotfix/*` normally contain `fix:` commits. The branch type
describes the delivery path: a bugfix waits for the next planned release, while
a hotfix produces an immediate patch release from `main`.

## Validation

Before merging, run the commands in the README's Validate section and audit
the changed Python scope:

```sh
python3 skills/review-code-quality/scripts/audit_python.py --git-diff
```
