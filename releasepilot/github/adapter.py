from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from releasepilot.core.adapters import PullRequestAdapter
from releasepilot.core.models import ChangedFile, PullRequest


class GitHubApiAdapter(PullRequestAdapter):
    def __init__(self, repo: str, token: str, api_base: str = "https://api.github.com") -> None:
        self.repo = repo
        self.token = token
        self.api_base = api_base.rstrip("/")

    @classmethod
    def from_env(cls) -> "GitHubApiAdapter | None":
        repo = os.getenv("RELEASEPILOT_GITHUB_REPO")
        token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
        if not repo or not token:
            return None
        return cls(repo=repo, token=token)

    def find_pr_for_commit(self, commit_sha: str) -> PullRequest | None:
        try:
            pulls = self._request_json(f"/repos/{self.repo}/commits/{commit_sha}/pulls")
        except (urllib.error.URLError, RuntimeError, json.JSONDecodeError):
            return None

        if not pulls:
            return None
        return self._build_pull_request(int(pulls[0]["number"]))

    def _build_pull_request(self, pr_number: int) -> PullRequest:
        pr_data = self._request_json(f"/repos/{self.repo}/pulls/{pr_number}")
        files = self._paginate(f"/repos/{self.repo}/pulls/{pr_number}/files", params={"per_page": "100"})
        return PullRequest(
            number=pr_number,
            title=pr_data.get("title", "").strip(),
            body=pr_data.get("body") or "",
            labels=[lbl.get("name", "") for lbl in pr_data.get("labels", []) if lbl.get("name")],
            files=[
                ChangedFile(
                    path=f.get("filename", ""),
                    status=f.get("status", "modified"),
                    patch=f.get("patch") or "",
                )
                for f in files
                if f.get("filename")
            ],
            base_ref=(pr_data.get("base") or {}).get("ref", ""),
            head_ref=(pr_data.get("head") or {}).get("ref", ""),
        )

    def _paginate(self, path: str, params: dict[str, str] | None = None) -> list[dict]:
        page = 1
        results: list[dict] = []
        while True:
            merged_params = {"page": str(page)}
            if params:
                merged_params.update(params)
            page_data = self._request_json(path, params=merged_params)
            if not page_data:
                break
            if not isinstance(page_data, list):
                raise RuntimeError(f"Expected list payload for {path}, got {type(page_data)}")
            results.extend(page_data)
            if len(page_data) < int(merged_params.get("per_page", "30")):
                break
            page += 1
        return results

    def _request_json(self, path: str, params: dict[str, str] | None = None) -> dict | list:
        qs = f"?{urllib.parse.urlencode(params)}" if params else ""
        url = f"{self.api_base}{path}{qs}"
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "releasepilot",
            },
            method="GET",
        )
        with urllib.request.urlopen(req) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
