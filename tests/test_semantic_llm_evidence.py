"""LLM prompts use IntentumDiff's semantic diff instead of raw patches.

Every model-facing path (summary prompt, single/batch/full-diff suggestions, tool retrieval,
change summaries, commit intent and Conventional Commit drafting) must send the semantic diff
when IntentumDiff reviewed the change, and never the raw patch text. The privacy mode only
decides whether IntentumDiff's change descriptions (which may contain literal values) are
included.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from verlato.cli.main import (
    _build_full_diff_suggestion_batch_prompt,
    _build_full_diff_suggestion_payload_item,
    _build_suggestion_source_evidence,
    _SuggestionWork,
)
from verlato.core.change_summary import build_change_summary_payload
from verlato.core.classifier import Classifier
from verlato.core.commit_intent import (
    build_conventional_commit_draft_prompt,
    build_diff_conventional_commit_prompt,
    commit_diff_intent_evidence,
    conventional_commit_draft_evidence,
)
from verlato.core.diff_evidence import DiffEvidenceBuilder
from verlato.core.models import ChangedFile, ChangeGroup, Commit, changed_files_for_group
from verlato.core.semantic import CommitSemantics, FileSemantics, SymbolChange
from verlato.core.suggestion_tools import SuggestionEvidenceToolProvider

RAW = "RAW_PATCH_MARKER"
LITERAL = "LITERAL_VALUE_42"
PATCH = f"diff --git a/src/app.py b/src/app.py\n@@ -1,2 +1,3 @@\n-def greet(name):\n+def greet(name, punct): # {RAW}\n"


def _commit() -> Commit:
    commit = Commit(
        sha="abc1234567890",
        message="update app",
        parent_sha="p0",
        files=[ChangedFile(path="src/app.py", status="modified", patch=PATCH)],
    )
    commit.semantics = CommitSemantics(
        sha=commit.sha,
        parent_sha="p0",
        engine_version="test",
        files=[
            FileSemantics(
                path="src/app.py",
                old_path="src/app.py",
                language="python",
                group_counts={"MEANINGFUL_CHANGE": 2, "REFACTORING": 1, "IGNORED_STYLE": 1},
                meaningful_change_types={"ADDITION": 1, "MODIFICATION": 1},
                has_semantic_changes=True,
                symbols=[
                    SymbolChange(name="greet", change="signature", file="src/app.py"),
                    SymbolChange(name="export_csv", change="added", file="src/app.py"),
                ],
                changes=[
                    {
                        "group": "meaningful",
                        "symbol": "greet",
                        "old_symbol": None,
                        "refactoring": None,
                        "edits": [
                            {"change": "ADDITION", "node": "default_parameter", "name": "punct",
                             "detail": "Insert -> default_parameter('punct')"},
                            {"change": "MODIFICATION", "node": "integer", "name": None,
                             "detail": f"{LITERAL} -> 43"},
                        ],
                    },
                    {
                        "group": "refactoring",
                        "symbol": "baz",
                        "old_symbol": "bar",
                        "refactoring": "RENAME_SYMBOL",
                        "edits": [{"change": "REFACTORING", "node": "function_definition", "name": "baz",
                                   "old_name": "bar", "detail": "Rename function_definition('bar') -> ('baz')"}],
                    },
                ],
            )
        ],
    )
    return commit


def _group(commit: Commit | None = None) -> ChangeGroup:
    commit = commit or _commit()
    return ChangeGroup(pr=None, commits=[commit], files=list(commit.files), grouping_reason="test")


def _diff(group: ChangeGroup):
    return DiffEvidenceBuilder().build(changed_files_for_group(group), commits=group.commits)


@pytest.mark.parametrize("detailed", [False, True])
def test_summary_prompt_uses_semantic_diff_never_raw_hunks(detailed: bool):
    group = _group()
    diff = _diff(group)
    result = Classifier()._build_summary_prompt_with_budget(
        title="update app",
        labels=[],
        pr_body="",
        diff=diff,
        commits=group.commits,
        include_hunks=detailed,
    )
    prompt = result.prompt
    assert RAW not in prompt
    assert "Representative hunks" not in prompt
    assert f"Semantic diff (IntentumDiff, {'detailed' if detailed else 'structure'}):" in prompt
    assert "meaningful in greet: addition default_parameter punct" in prompt
    assert "refactoring RENAME_SYMBOL: bar -> baz" in prompt
    assert "ignored as formatting/noise (style:1)" in prompt
    assert "never present moved code, refactorings or formatting as new behavior" in prompt
    assert (LITERAL in prompt) is detailed
    assert result.budget["evidence_source"] == "intentumdiff-semantic-diff"
    assert result.budget["raw_patch_text_included"] is False
    assert result.budget["raw_patch_chars_sent"] == 0
    assert "diff excerpts" not in diff.summary


def test_legacy_git_engine_still_uses_hunks_when_allowed():
    commit = _commit()
    commit.semantics = None
    group = _group(commit)
    result = Classifier()._build_summary_prompt_with_budget(
        title="update app", labels=[], pr_body="", diff=_diff(group), commits=group.commits, include_hunks=True,
    )
    assert "Representative hunks" in result.prompt
    assert RAW in result.prompt
    assert result.budget["evidence_source"] == "git-patch"


@pytest.mark.parametrize("detailed", [False, True])
def test_full_diff_payload_replaces_patches_with_semantic_diff(detailed: bool):
    group = _group()
    diff = _diff(group)
    item = Classifier().classify(group, "base", "head")[0]
    payload = _build_full_diff_suggestion_payload_item(group, item, diff, include_patches=detailed)
    text = json.dumps(payload)
    assert RAW not in text
    assert payload["evidence_source"] == "intentumdiff-semantic-diff"
    assert payload["patch_chars_sent"] == 0
    assert payload["semantic_review"]["signature_changes"] == ["greet"]
    file_payload = payload["files"][0]
    assert "patch" not in file_payload
    assert file_payload["semantic_diff"][0]["changes"][0]["symbol"] == "greet"
    assert (LITERAL in text) is detailed

    work = _SuggestionWork.__new__(_SuggestionWork)
    work.suggestion_count = 1
    work.suggestion_tone = "release-note"
    work.suggestion_style = {}
    work.full_diff_item = payload
    work.full_diff_patch_chars = 0
    prompt = _build_full_diff_suggestion_batch_prompt([work])
    assert "from the IntentumDiff semantic diff" in prompt
    assert "Use each file's semantic_diff" in prompt
    assert "file patches" not in prompt


def test_tool_retrieval_offers_semantic_diff_tools_not_patch_tools():
    group = _group()
    item = Classifier().classify(group, "base", "head")[0]
    provider = SuggestionEvidenceToolProvider.from_groups([group], [item])
    names = [tool.name for tool in provider.list_tools()]
    assert "verlato_get_file_semantic_diff" in names and "verlato_search_semantic_diff" in names
    assert "verlato_get_file_patch" not in names and "verlato_search_item_diff" not in names
    manifest = provider.call_tool("verlato_get_suggestion_item", {"item_id": item.id})
    assert manifest["evidence_source"] == "intentumdiff-semantic-diff"
    assert manifest["semantic_review"]["verdict"] == "behavioral"
    result = provider.call_tool("verlato_get_file_semantic_diff", {"item_id": item.id, "path": "src/app.py"})
    assert result["detail"] == "structure"
    assert "meaningful in greet" in result["text"]
    assert RAW not in json.dumps(result) and LITERAL not in json.dumps(result)
    found = provider.call_tool("verlato_search_semantic_diff", {"item_id": item.id, "query": "baz"})
    assert found["results"][0]["change"].startswith("- refactoring RENAME_SYMBOL: bar -> baz")
    detailed = SuggestionEvidenceToolProvider.from_groups([group], [item], semantic_detail=True)
    assert LITERAL in detailed.get_file_semantic_diff(item_id=item.id, path="src/app.py")["text"]


def test_source_evidence_and_change_summary_use_semantic_diff():
    group = _group()
    diff = _diff(group)
    evidence = _build_suggestion_source_evidence(diff, group, include_hunks=True)
    kinds = [entry["kind"] for entry in evidence]
    assert "semantic-diff" in kinds and "diff" not in kinds
    assert RAW not in json.dumps(evidence)

    summary = build_change_summary_payload(group=group, diff=diff)
    assert summary["evidence_source"] == "intentumdiff-semantic-diff"
    assert summary["files"][0]["public_symbols"] == ["export_csv", "greet"]
    assert summary["semantic_diff"][0]["changes"][0]["symbol"] == "greet"
    assert LITERAL not in json.dumps(summary)  # change summaries are always structure-level


@pytest.mark.parametrize("include_patches", [False, True])
def test_commit_intent_and_conventional_drafts_use_semantic_diff(include_patches: bool):
    commit = _commit()
    for evidence in (
        commit_diff_intent_evidence(commit, include_patches=include_patches),
        conventional_commit_draft_evidence(commit, include_patches=include_patches),
    ):
        text = json.dumps(evidence)
        assert RAW not in text
        assert "patch_excerpt" not in text
        assert evidence["raw_patch_chars_sent"] == 0
        assert evidence["evidence_source"] == "intentumdiff-semantic-diff"
        assert evidence["files"][0]["semantic_diff"][0]["changes"][0]["symbol"] == "greet"
        assert (LITERAL in text) is include_patches
    facts = conventional_commit_draft_evidence(commit)["diff_facts"]
    assert facts["source"] == "intentumdiff"
    assert facts["public_symbols"] == ["src/app.py:greet", "src/app.py:export_csv"]
    assert "punct" in facts["added_terms"]
    prompt = build_conventional_commit_draft_prompt(commit, include_patches=include_patches)
    instructions = prompt.split("COMMIT_DRAFT_EVIDENCE:")[0]
    assert "IntentumDiff semantic diff" in instructions and "patch excerpts" not in instructions
    assert "IntentumDiff semantic diff" in build_diff_conventional_commit_prompt(commit)


@pytest.mark.skipif(importlib.util.find_spec("intentumdiff") is None, reason="intentumdiff-python is not installed")
def test_draft_prompt_end_to_end_sends_semantic_diff(monkeypatch, tmp_path: Path, capsys):
    from verlato.cli.main import main

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)

    git("init")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "Tester")
    (repo / "app.py").write_text("def greet(name):\n    return 'hi ' + name\n\nTIMEOUT = 30\n")
    git("add", ".")
    git("commit", "-m", "init")
    git("tag", "v1")
    (repo / "app.py").write_text("def greet(name, punct='!'):\n    return 'hi ' + name + punct\n\nTIMEOUT = 60\n")
    git("commit", "-am", "update app")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("VERLATO_MODEL_PROVIDER", "stub")
    monkeypatch.setenv("VERLATO_STATE_PATH", str(tmp_path / "state.json"))
    assert main(["scan", "--base", "v1"]) == 0
    item_id = json.loads(capsys.readouterr().out)[0]["id"]

    for privacy, detailed in (("metadata-only", False), ("local-summary-then-ai", True)):
        assert main(["draft", "prompt", "--base", "v1", "--id", item_id, "--model-provider", "ollama",
                     "--privacy", privacy]) == 0
        output = json.loads(capsys.readouterr().out)
        prompt = output["prompt"]
        assert "Semantic diff (IntentumDiff" in prompt
        assert "meaningful in greet: addition default_parameter punct" in prompt
        assert "@@" not in prompt and "+def greet" not in prompt
        assert ("(was `30`, now `60`)" in prompt) is detailed


def test_suggestion_parser_keeps_multi_line_entries():
    from verlato.cli.main import _suggestion_candidates

    entry = "**Pagination fix**\n- Fixes `paginate()` skipping the first item of every page.\n- Page counts are unchanged."
    assert _suggestion_candidates(entry, 1) == [entry]
    variants = "1. Adds CSV export.\n   Use `--csv` to write a report.\n2. Adds a CSV report option."
    assert _suggestion_candidates(variants, 2) == [
        "Adds CSV export.\n   Use `--csv` to write a report.",
        "Adds a CSV report option.",
    ]
    assert _suggestion_candidates("1. Renames get_user to fetch_user.", 1) == ["Renames get_user to fetch_user."]
    assert _suggestion_candidates("```\nAdds export.\n```", 1) == ["Adds export."]
