from __future__ import annotations

from collections import defaultdict
from dataclasses import replace

from verlato.core.commit_intent import effective_conventional_commit
from verlato.core.diff_evidence import DiffEvidenceBuilder
from verlato.core.models import ChangedFile, ChangeGroup, Commit, PullRequest


class Grouper:
    USER_FACING_AREAS = {"api", "frontend", "backend", "infra", "general"}

    def __init__(self) -> None:
        self.diff_builder = DiffEvidenceBuilder()

    def group(self, commits: list[Commit], prs_by_commit: dict[str, PullRequest]) -> list[ChangeGroup]:
        grouped: dict[int, list[Commit]] = defaultdict(list)
        no_pr: list[Commit] = []
        for commit in commits:
            pr = prs_by_commit.get(commit.sha)
            if pr:
                grouped[pr.number].append(commit)
            else:
                no_pr.append(commit)

        results: list[ChangeGroup] = []
        for _pr_number, pr_commits in grouped.items():
            pr = prs_by_commit[pr_commits[0].sha]
            results.extend(self._split_pr_group(pr, pr_commits))

        results.extend(self._group_non_pr_commits(no_pr))

        return results

    def _group_non_pr_commits(self, commits: list[Commit]) -> list[ChangeGroup]:
        if not commits:
            return []

        groups: list[ChangeGroup] = []
        bucket: list[Commit] = []
        bucket_key: tuple[str, ...] | None = None
        bucket_paths: set[str] = set()

        for commit in commits:
            key = self._non_pr_group_key(commit)
            commit_paths = self._substantive_paths(commit.files)
            if not bucket:
                bucket = [commit]
                bucket_key = key
                bucket_paths = set(commit_paths)
                continue
            if bucket_key == key and key[0] != "single":
                bucket.append(commit)
                bucket_paths.update(commit_paths)
                continue
            overlap = bucket_paths & commit_paths
            if overlap:
                bucket.append(commit)
                bucket_paths.update(commit_paths)
                bucket_key = ("file-overlap", *sorted(overlap)[:5])
                continue
            groups.append(
                ChangeGroup(
                    pr=None,
                    commits=bucket,
                    files=self._commit_files(bucket),
                    grouping_reason=self._non_pr_group_reason(bucket_key),
                )
            )
            bucket = [commit]
            bucket_key = key
            bucket_paths = set(commit_paths)

        if bucket:
            groups.append(
                ChangeGroup(
                    pr=None,
                    commits=bucket,
                    files=self._commit_files(bucket),
                    grouping_reason=self._non_pr_group_reason(bucket_key),
                )
            )
        return groups

    def _non_pr_group_key(self, commit: Commit) -> tuple[str, ...]:
        parsed, used_inferred = effective_conventional_commit(commit)
        if parsed:
            if parsed.scope:
                if used_inferred:
                    return ("inferred-conventional-scope", parsed.type.lower(), parsed.scope.lower())
                return ("conventional-scope", parsed.type.lower(), parsed.scope.lower())
            return ("single", commit.sha)

        return ("single", commit.sha)

    def _non_pr_group_reason(self, key: tuple[str, ...] | None) -> str:
        if not key:
            return "Grouped as an individual non-PR commit."
        if key[0] == "conventional-scope":
            return f"Grouped because these non-PR commits share conventional type `{key[1]}` and scope `{key[2]}`."
        if key[0] == "inferred-conventional-scope":
            return (
                "Grouped because these non-PR commits share inferred conventional "
                f"type `{key[1]}` and scope `{key[2]}`."
            )
        if key[0] == "file-overlap":
            files = ", ".join(key[1:4])
            suffix = f": {files}" if files else "."
            return f"Grouped because adjacent non-PR commits touch overlapping substantive files{suffix}"
        return "Kept separate because no PR or explicit conventional scope was found."

    def _split_pr_group(self, pr: PullRequest, commits: list[Commit]) -> list[ChangeGroup]:
        scoped_groups = self._split_pr_group_by_scope(pr, commits)
        if scoped_groups:
            return scoped_groups

        commit_files = self._commit_files(commits)
        if not pr.files:
            return [
                ChangeGroup(
                    pr=pr,
                    commits=commits,
                    files=commit_files,
                    grouping_reason=f"Grouped by PR #{pr.number}.",
                )
            ]

        diff = self.diff_builder.build(pr.files)
        user_facing_areas = [a for a in diff.files_by_area if a in self.USER_FACING_AREAS]
        if len(user_facing_areas) <= 1:
            return [
                ChangeGroup(
                    pr=pr,
                    commits=commits,
                    files=commit_files,
                    grouping_reason=f"Grouped by PR #{pr.number}.",
                )
            ]

        split_groups: list[ChangeGroup] = []
        for area in sorted(user_facing_areas):
            files = [f for f in pr.files if self.diff_builder.infer_area(f.path) == area]
            area_commit_files = [f for f in commit_files if self.diff_builder.infer_area(f.path) == area]
            if not files:
                continue
            area_pr = replace(pr, title=f"{pr.title} [{area}]", files=[ChangedFile(**f.__dict__) for f in files])
            split_groups.append(
                ChangeGroup(
                    pr=area_pr,
                    commits=commits,
                    files=area_commit_files,
                    grouping_reason=f"Grouped by PR #{pr.number}, then split by changed area `{area}`.",
                )
            )

        return split_groups or [ChangeGroup(pr=pr, commits=commits, grouping_reason=f"Grouped by PR #{pr.number}.")]

    def _split_pr_group_by_scope(self, pr: PullRequest, commits: list[Commit]) -> list[ChangeGroup]:
        if len(commits) < 2:
            return []

        buckets: dict[str, list[Commit]] = defaultdict(list)
        inferred_scopes = False
        for commit in commits:
            parsed, used_inferred = effective_conventional_commit(commit)
            if not parsed or not parsed.scope:
                return []
            inferred_scopes = inferred_scopes or used_inferred
            buckets[parsed.scope.lower()].append(commit)

        if len(buckets) <= 1:
            return []

        groups: list[ChangeGroup] = []
        for scope, scope_commits in buckets.items():
            files = self._commit_files(scope_commits)
            scope_pr = replace(
                pr,
                title=f"{pr.title} [{scope}]",
                files=[ChangedFile(**f.__dict__) for f in files],
            )
            groups.append(
                ChangeGroup(
                    pr=scope_pr,
                    commits=scope_commits,
                    files=files,
                    grouping_reason=(
                        f"Grouped by PR #{pr.number}, then split because the PR contains "
                        f"multiple {'inferred or explicit' if inferred_scopes else 'explicit'} "
                        f"conventional scopes; this changeset uses scope `{scope}`."
                    ),
                )
            )
        return groups

    @classmethod
    def _commit_files(cls, commits: list[Commit]) -> list[ChangedFile]:
        files: list[ChangedFile] = []
        by_path: dict[str, ChangedFile] = {}
        for commit in commits:
            for file in commit.files:
                if file.path in by_path:
                    existing = by_path[file.path]
                    if file.patch and file.patch not in existing.patch:
                        existing.patch = "\n".join(part for part in (existing.patch, file.patch) if part)
                    if file.status:
                        existing.status = file.status
                    continue
                changed_file = ChangedFile(path=file.path, status=file.status, patch=file.patch)
                files.append(changed_file)
                by_path[file.path] = changed_file
        return files

    @classmethod
    def _substantive_paths(cls, files: list[ChangedFile]) -> set[str]:
        return {file.path for file in files if file.path and not cls._is_generated_release_artifact(file.path)}

    @staticmethod
    def _is_generated_release_artifact(path: str) -> bool:
        normalized = path.replace("\\", "/").lower()
        return normalized in {
            "changelog.md",
            ".github/release.md",
            ".verlato_state.json",
            ".verlato_suggestion_cache.json",
        } or normalized.endswith((".vsix", ".tgz"))
