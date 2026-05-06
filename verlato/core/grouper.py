from __future__ import annotations

from collections import defaultdict
from dataclasses import replace

from verlato.core.conventional import parse_conventional_commit
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

        for commit in commits:
            key = self._non_pr_group_key(commit)
            if not bucket:
                bucket = [commit]
                bucket_key = key
                continue
            if bucket_key == key and key[1] != "misc":
                bucket.append(commit)
                continue
            groups.append(ChangeGroup(pr=None, commits=bucket, files=self._commit_files(bucket)))
            bucket = [commit]
            bucket_key = key

        if bucket:
            groups.append(ChangeGroup(pr=None, commits=bucket, files=self._commit_files(bucket)))
        return groups

    def _non_pr_group_key(self, commit: Commit) -> tuple[str, ...]:
        parsed = parse_conventional_commit(commit.message)
        if parsed:
            scope = parsed.scope or "*"
            return ("conventional", parsed.type.lower(), scope.lower(), commit.author.lower())

        subject = commit.message.strip().splitlines()[0].lower() if commit.message.strip() else ""
        category = "misc"
        for prefix in ("docs", "test", "chore", "refactor", "ci", "build", "fix", "feat"):
            if subject.startswith(prefix):
                category = prefix
                break
        return ("heuristic", category, commit.author.lower())

    def _split_pr_group(self, pr: PullRequest, commits: list[Commit]) -> list[ChangeGroup]:
        if not pr.files:
            return [ChangeGroup(pr=pr, commits=commits, files=self._commit_files(commits))]

        diff = self.diff_builder.build(pr.files)
        user_facing_areas = [a for a in diff.files_by_area if a in self.USER_FACING_AREAS]
        if len(user_facing_areas) <= 1:
            return [ChangeGroup(pr=pr, commits=commits)]

        split_groups: list[ChangeGroup] = []
        for area in sorted(user_facing_areas):
            files = [f for f in pr.files if self.diff_builder.infer_area(f.path) == area]
            if not files:
                continue
            area_pr = replace(pr, title=f"{pr.title} [{area}]", files=[ChangedFile(**f.__dict__) for f in files])
            split_groups.append(ChangeGroup(pr=area_pr, commits=commits))

        return split_groups or [ChangeGroup(pr=pr, commits=commits)]

    @staticmethod
    def _commit_files(commits: list[Commit]) -> list[ChangedFile]:
        files: list[ChangedFile] = []
        seen: set[tuple[str, str, str]] = set()
        for commit in commits:
            for file in commit.files:
                key = (file.path, file.status, file.patch)
                if key in seen:
                    continue
                files.append(ChangedFile(path=file.path, status=file.status, patch=file.patch))
                seen.add(key)
        return files
