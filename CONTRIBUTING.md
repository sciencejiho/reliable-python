# Contributing

Thanks for helping improve reliable-python. This document describes how to
report issues, propose changes, and get a pull request merged.

## Contents

- [Found an Issue or Bug?](#issue)
- [Missing a Feature?](#feature)
- [Want a Doc Fix?](#docs)
- [Issue Submission Guidelines](#submit)
- [Branches](#branches)
- [Releases](#releases)
- [Commits](#commits)
- [Titles](#titles)
- [Pull Request Submission Guidelines](#submit-pr)
- [Validation](#validation)

---

## <a name="issue"></a> Found an Issue or Bug?

Report it in the [issue tracker][issues]. Even better, open a pull request with
a fix.

Two categories of report are especially useful here:

- **False positives.** The checker reported a finding that is not a real
  problem. Include the smallest Python snippet that triggers it. False
  positives are defects: every one of them costs a contributor a suppression
  comment, and suppressions that are added reflexively make the whole mechanism
  untrustworthy.
- **False negatives.** Code that plainly violates a rule and was not reported.

**Please see the [Issue Submission Guidelines](#submit) below.**

## <a name="feature"></a> Missing a Feature?

Open an issue describing the problem before writing code.

- **A new rule or skill** should be discussed in an issue first. Rules are hard
  to remove once people depend on them, and a rule that fires too often trains
  contributors to ignore the checker. State the failure mode the rule catches
  and estimate its false-positive rate on real code.
- **Small changes** can go straight to a pull request.

## <a name="docs"></a> Want a Doc Fix?

Documentation changes are welcome as direct pull requests. For anything that
changes what a rule *means*, update the skill, the reference under
`skills/using-power-of-ten/references/`, and `README.md` together, so the three
never disagree.

---

## <a name="submit"></a> Issue Submission Guidelines

Search existing issues before opening a new one.

Include what applies:

- **Overview**: what happened, and what you expected instead
- **Version**: the plugin version from `.claude-plugin/plugin.json`, or the
  commit hash
- **Environment**: Python version, operating system, and which host (Claude
  Code or Codex)
- **Reproduction**: the smallest Python snippet that shows the behavior
- **Actual output**: the exact checker output, from
  `python3 skills/review-code-quality/scripts/audit_python.py <paths>`
- **Suggested fix (optional)**: suspected cause or relevant code areas

---

## <a name="branches"></a> Branches

This project uses Git Flow. The production branch is named `main`.

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

When work depends on an unmerged branch, stack it: branch from the dependency
and target the pull request at it, rather than at `develop`. Say so in the
pull request body.

## <a name="releases"></a> Releases

**A release originates from `main`, and only from `main`, via an annotated tag.**

There is exactly one way a version becomes published:

1. Branch `release/X.Y.Z` from `develop`, or `hotfix/X.Y.Z` from the latest
   released commit on `main`.
2. Bump the version and update documentation on that branch. Nothing else.
3. Merge it into `main` with a non-fast-forward merge.
4. Tag that commit on `main`: `git tag -a vX.Y.Z -m "vX.Y.Z"`.
5. Merge the same branch into `develop` so the bump is not lost.
6. Publish a GitHub Release from the tag, targeting `main`.

Never tag `develop`, a `feature/*` branch, or a `release/*` branch that has not
yet reached `main`. A tag that is not reachable from `main` is not a release,
and anything built or installed from it is unreproducible — the branch it came
from can still be rewritten, but `main` cannot.

### Version locations

A version bump edits five places, and only one of them is enforced by a test:

1. `.claude-plugin/plugin.json`
2. `.codex-plugin/plugin.json`
3. `.claude-plugin/marketplace.json`
4. `PackageConsistencyTests` in `tests/test_audit_python.py` — hardcodes the
   expected version and fails on drift
5. The version badge in `README.md` — **not covered by any test**

### Release notes

Every tag gets a GitHub Release whose notes are written from what actually
changed, by comparing the two tags — not from the commit subjects alone. Use
the sections that apply:

- **Summary** — what this release is for, in a few sentences. If it is a
  hardening or maintenance release with no new rules, say so plainly rather
  than dressing it up.
- **New Features** — capabilities that did not exist before
- **Bug Fixes** — defects closed, each naming the failure it prevents
- **Upgrading** — only when a user has to do something, or when existing
  behavior changes

State honestly when a release adds nothing new. `v0.2.1` shipped identical rule
codes to `v0.2.0` and was a hardening release; its notes say exactly that.

## <a name="commits"></a> Commits

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

## <a name="titles"></a> Titles

Issue and pull request titles use the same Conventional Commits grammar as
commit subjects, so one change reads consistently from issue to branch to
commit to pull request.

- **Enhancement and task issues** take the type the eventual commit will carry:
  `feat: enforce a NumPy-default docstring convention`.
- **Pull requests** take the type of the change they deliver.
- **Bug reports are the exception.** Title them with the symptom, in plain
  prose: `Checker crashes on except* handlers`, not
  `fix: handle except* handlers`. An issue is a problem statement; the type
  belongs on the commit that answers it, and prefixing the report with `fix:`
  quietly commits the maintainer to a solution nobody has designed yet.

If the type is not obvious, prefer the one describing the user-visible outcome
and treat the hesitation as a sign the issue needs sharper scope.

---

## <a name="submit-pr"></a> Pull Request Submission Guidelines

- Search existing [pull requests][pulls] to avoid duplicate work.
- Create a branch from `develop`:

  ```sh
  git checkout -b feature/my-change develop
  ```

- Make the change, with test cases where behavior changes.
- Follow the rules the plugin itself enforces. This repository is audited by
  its own checker and must stay at zero findings, which constrains how code is
  written here: every loop needs a visible bound, functions cap at 60 code
  lines, and a suppression needs a specific rationale.
- Run the full validation set below.
- Push the branch and open a pull request against `develop`.

If a maintainer requests changes, update the branch, re-run validation, and
push again. Amending and force-pushing is fine; so is adding commits when the
iterations are worth seeing side by side.

### Adding or changing a rule

A rule change needs more than a passing test:

- A test for the case it catches **and** a test for the nearest case it must
  not catch. A rule without a negative test will find false positives in
  production instead.
- Its identifier documented in `README.md` and `CLAUDE.md`.
- A `remedy` on every finding, naming the smallest safe correction. A finding
  without an actionable remedy is noise.
- The repository still clean under both the local checker and the released one
  installed in the plugin cache, if the change alters an existing rule's
  meaning.

---

## <a name="validation"></a> Validation

Run all of these before opening a pull request:

```sh
# Tests
python3 -m unittest discover -s tests -v

# The repository must audit clean against its own checker
python3 skills/review-code-quality/scripts/audit_python.py hooks tests skills

# Changed-scope audit, matching what the completion gate runs
python3 skills/review-code-quality/scripts/audit_python.py --git-diff

# Manifest validation
claude plugin validate .
```

There is no separate linter or type checker configured; the checker and the
unittest suite are the whole validation story.

[issues]: https://github.com/sciencejiho/reliable-python/issues
[pulls]: https://github.com/sciencejiho/reliable-python/pulls
