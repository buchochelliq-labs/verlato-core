# Concepts

## Ranges

Every Verlato command that looks at history works on a **range**: the commits reachable from
`--head` (default `HEAD`) but not from `--base`, like `git log base..head`. Choose the base
one of three ways:

| Option | Base |
|---|---|
| `--base <ref>` | any tag, branch or commit |
| `--from-latest-tag` | the latest tag reachable from `--head` |
| `--from-latest-published` | the most recent version recorded with `verlato state mark-published` (it must also be a git ref, so record tag names) |

Give exactly one. The VS Code extension's Control view has the same base and head pickers.

## From commits to release notes

```text
commits ──► changesets ──► change items ──► draft ──► CHANGELOG.md / release notes
         group          classify       review        apply
```

1. **Collect.** Verlato reads the range's commits with git, along with their changed files.
   With the default IntentumDiff engine it also reviews each commit semantically (see
   [Semantic evidence](../technical/semantic-evidence.md)). Reviews are cached per commit in
   `.verlato_semantic_cache.json`, so later runs only review new commits.
2. **Group.** Commits become **changesets** (also called groups). A merged pull request is
   one changeset. Commits outside pull requests are grouped when they share a Conventional
   Commit type and scope, or when adjacent commits touch overlapping files; the reason is
   recorded as the item's `grouping_reason`. Which commits belong to which PR comes from
   merge commits (`standard-git`, the default) or, optionally, the GitHub API (see
   [PR mapping](#pull-request-mapping)).
3. **Classify.** Each changeset becomes a **change item** with:
    - a **kind**, which decides its section: Breaking Changes, Added, Changed, Deprecated,
      Removed, Fixed, Security, Documentation, Performance, Refactoring, Tests, Build,
      Internal, Other;
    - an **area** (`api`, `frontend`, `backend`, `docs`, `tests`, `infra` or `general`), from
      PR labels or the changed paths;
    - an **audience** and a **SemVer impact** (`major`, `minor`, `patch`, `none`);
    - a **title** and **summary**;
    - a **confidence** between 0 and 1;
    - its **evidence**: labels, files, commit signals, and the semantic review.

    Signals are weighed in this order: authored Conventional Commits and PR labels first, then
    the semantic review, then changed-file statuses and paths. A model, if one is configured,
    can improve the summary and help with low-confidence classifications, but never decides
    on its own.
4. **Draft.** The items for a range form a **draft**: sections of items, plus the rendered
   Markdown for a changelog and for GitHub release notes.
5. **Review and apply.** You accept, edit or hide items, then write the draft into a target
   file.

## Review status

Every item has one of these statuses:

| Status | Meaning |
|---|---|
| `needs-review` | Verlato is not confident enough (confidence below 0.8), or the evidence needs a human look |
| `ready` | confident enough to publish as is (confidence 0.8 or more) |
| `accepted` | a reviewer approved it |
| `edited` | a reviewer replaced the summary (set automatically when you save a summary) |
| `hidden` | left out of the rendered notes, but kept in the draft data |

The semantic review routes items to `needs-review` whatever their messages say:

- **possible API breaks**: public functions, methods or classes removed, renamed or
  re-signatured outside tests (`semantic:api-break-candidate`);
- **guardrail violations**: protected settings changed, per the repository's
  `intentumdiff.yaml` (`semantic:guardrail`);
- **contradictions**: a `feat:`, `fix:` or `perf:` commit whose changes are formatting only
  (`semantic:conflict`).

## State

Verlato keeps its memory in `.verlato_state.json` at the repository root (change with
`state_path` or `VERLATO_STATE_PATH`):

- **items** that were generated or reviewed, with their review status, reviewer, edited
  summary and the provenance of applied model suggestions;
- **publication**: whether each item was published and in which release (`published_in`),
  which also gives each item its area's release **lineage**;
- **feedback events** from reviews and suggestions (hashes and metrics by default; see
  [Writing style and vocabulary](style.md#feedback)).

Items are matched by a **checksum** of their PR, commits, title and area, so re-running a
scan
never duplicates them, and review decisions survive later runs. Commands that change state:
`generate`, `draft review`, `draft suggest` (when applying), `feedback record`,
`state mark-published` and `state clear-working`. Everything else only reads.

Other files Verlato may create next to it: `.verlato_semantic_cache.json` (semantic reviews)
and `.verlato_suggestion_cache.json` (model suggestions). All three are local working files;
most projects add `.verlato_*` to `.gitignore`.

## Targets

A **target** is a file the draft is applied to. Two are built in, and both can be configured
in [`verlato.json`](../reference/configuration.md#targets):

| Target | Default path | Format |
|---|---|---|
| `changelog` | `CHANGELOG.md` | Keep a Changelog-style entries (`### v1.1.0 (date)`, `### Added`, ...) |
| `github-release` | `.github/release.md` | GitHub release notes (`## v1.1.0 - date`, `### Added`, ...) |

`draft apply` creates the file or adds the new release to it.

## Pull-request mapping

`VERLATO_PR_ADAPTER` chooses how commits are matched to pull requests:

- `standard-git` (default): from merge commits such as "Merge pull request #12 from ...".
  Squash-merged PRs are recognised by a trailing `(#12)` in the subject.
- `github`: the GitHub REST API (read-only), which also brings in PR titles, bodies and
  labels. It needs `VERLATO_GITHUB_REPO=owner/repo` and `GITHUB_TOKEN` (or `GH_TOKEN`);
  without them Verlato falls back to `standard-git`.
- `none`: no PR mapping; every changeset comes from commit grouping.

## Engines

The **diff engine** decides where change evidence comes from:

- `intentumdiff` (default): the semantic review described above.
- `git`: legacy text-patch evidence (changed-file statuses, paths and representative hunks).
  No parser bundle needed, but no semantic signals either.

Choose with `diff_engine` in `verlato.json`, `VERLATO_DIFF_ENGINE`, the global `--diff-engine`
option, or the `verlato.diffEngine` VS Code setting.
