from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from verlato.core.adapters import NullPullRequestAdapter
from verlato.core.classifier import Classifier
from verlato.core.collector import Collector
from verlato.core.commit_intent import commit_diff_intent_evidence
from verlato.core.models import ChangedFile, ChangeGroup, Commit
from verlato.core.semantic import (
    CommitSemantics,
    FileSemantics,
    IntentumDiffEngine,
    SemanticCache,
    SemanticEngineUnavailable,
    SymbolChange,
    UnreviewedFile,
    build_semantic_engine,
    build_semantic_evidence,
    digest_file_diff,
    normalize_diff_engine,
    resolve_diff_engine,
)

HAS_INTENTUMDIFF = importlib.util.find_spec("intentumdiff") is not None


# ---------------------------------------------------------------------------
# helpers: IntentumDiff-shaped fakes
# ---------------------------------------------------------------------------


def node(label: str, node_type: str = "function_definition", *, definition: bool = True, params: int | None = None):
    facts = NS(param_count=params) if definition else None
    return NS(label=label, node_type=node_type, facts=facts)


def change(change_type: str, old=None, new=None, refactoring_kind=None):
    return NS(change_type=NS(value=change_type), old_node=old, new_node=new, refactoring_kind=refactoring_kind)


def group(kind: str, indices: list[int], old_labels=(), new_labels=(), refactoring_kind=None):
    return NS(
        kind=NS(value=kind),
        raw_change_indices=list(indices),
        old_labels=list(old_labels),
        new_labels=list(new_labels),
        refactoring_kind=refactoring_kind,
    )


def file_diff(changes, groups, *, language="python", style_only=False, guardrails=(), lifecycle="modified"):
    return NS(
        changes=changes,
        change_groups=groups,
        language=language,
        is_style_only=style_only,
        has_semantic_changes=bool(changes) and not style_only,
        is_fallback=False,
        guardrail_violations=list(guardrails),
        parse_errors=[],
        metadata={"file_lifecycle": lifecycle},
    )


def digest(sha: str, *files: FileSemantics, parent: str = "p0", unreviewed=(), error=None) -> CommitSemantics:
    return CommitSemantics(
        sha=sha,
        parent_sha=parent,
        engine_version="0.0.2b2",
        files=list(files),
        unreviewed=list(unreviewed),
        error=error,
    )


def semantic_commit(sha: str, message: str, *files: FileSemantics, paths: list[str] | None = None) -> Commit:
    commit = Commit(
        sha=sha,
        message=message,
        parent_sha="p0",
        files=[ChangedFile(path=p, status="modified") for p in (paths or [f.path for f in files])],
    )
    commit.semantics = digest(sha, *files)
    return commit


def fs(path: str, **kwargs) -> FileSemantics:
    return FileSemantics(path=path, old_path=path, language=kwargs.pop("language", "python"), **kwargs)


# ---------------------------------------------------------------------------
# engine selection
# ---------------------------------------------------------------------------


def test_diff_engine_defaults_to_intentumdiff(monkeypatch):
    monkeypatch.delenv("VERLATO_DIFF_ENGINE", raising=False)
    assert resolve_diff_engine(None) == "intentumdiff"
    assert resolve_diff_engine("git") == "git"
    assert normalize_diff_engine("legacy") == "git"
    assert normalize_diff_engine("semantic") == "intentumdiff"
    assert normalize_diff_engine("nonsense") == "intentumdiff"
    monkeypatch.setenv("VERLATO_DIFF_ENGINE", "git")
    assert resolve_diff_engine("intentumdiff") == "git"
    assert resolve_diff_engine("intentumdiff", requested="intentumdiff") == "intentumdiff"


def test_git_engine_builds_no_semantic_engine():
    assert build_semantic_engine("git") is None
    assert isinstance(build_semantic_engine("intentumdiff", cache_path=None), IntentumDiffEngine)


def test_missing_intentumdiff_raises_actionable_error(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "intentumdiff" or name.startswith("intentumdiff."):
            raise ImportError("No module named 'intentumdiff'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    engine = IntentumDiffEngine(".")
    with pytest.raises(SemanticEngineUnavailable, match="pip install --pre intentumdiff-python"):
        engine.ensure_available()
    info = engine.info()
    assert info["available"] is False
    assert "VERLATO_DIFF_ENGINE=git" in info["error"]


# ---------------------------------------------------------------------------
# digest extraction
# ---------------------------------------------------------------------------


def test_digest_extracts_definition_symbols_signatures_and_renames():
    changes = [
        change("ADDITION", new=node("punct", "default_parameter", definition=False)),
        change("DELETION", old=node("gone", params=1)),
        change("ADDITION", new=node("fresh", params=2)),
        change(
            "REFACTORING",
            old=node("bar", params=2),
            new=node("baz", params=2),
            refactoring_kind=NS(value="RENAME_SYMBOL"),
        ),
        change("ADDITION", new=node("[5]", "pair")),  # data-format node, not a named symbol
        change("ADDITION", new=node("pair", "pair")),  # label equals node type, not a named symbol
    ]
    groups = [
        group("MEANINGFUL_CHANGE", [0], old_labels=["greet"], new_labels=["greet", "punct"]),
        group("MEANINGFUL_CHANGE", [1]),
        group("MEANINGFUL_CHANGE", [2]),
        group("REFACTORING", [3], refactoring_kind=NS(value="RENAME_SYMBOL")),
        group("IGNORED_STYLE", []),
    ]
    digest_ = digest_file_diff(file_diff(changes, groups), path="src/app.py", old_path="src/app.py")
    by_change = {(symbol.change, symbol.name) for symbol in digest_.symbols}
    assert ("added", "fresh") in by_change
    assert ("removed", "gone") in by_change
    assert ("signature", "greet") in by_change
    assert ("renamed", "baz") in by_change
    assert not any(symbol.name in {"[5]", "pair"} for symbol in digest_.symbols)
    assert digest_.group_counts == {"MEANINGFUL_CHANGE": 3, "REFACTORING": 1, "IGNORED_STYLE": 1}
    assert digest_.refactorings == ["RENAME_SYMBOL"]
    assert digest_.meaningful_change_types == {"ADDITION": 2, "DELETION": 1}


def test_digest_never_stores_change_descriptions_or_literals():
    literal_change = change("MODIFICATION", old=node("'secret-v1'", "string", definition=False),
                            new=node("'secret-v2'", "string", definition=False))
    literal_change.description = "Update string('secret-v1') -> string('secret-v2')"
    literal_change.text_diff = "secret-v1 -> secret-v2"
    digest_ = digest_file_diff(
        file_diff([literal_change], [group("MEANINGFUL_CHANGE", [0], ["token"], ["token"])]),
        path="config.py",
        old_path="config.py",
    )
    assert "secret" not in json.dumps(digest_.__dict__, default=lambda value: value.__dict__)


def test_whole_file_additions_without_groups_count_as_meaningful():
    digest_ = digest_file_diff(
        file_diff([change("ADDITION", new=node("root", "document", definition=False))], [], language="yaml",
                  lifecycle="added"),
        path="settings.yaml",
        old_path="settings.yaml",
    )
    assert digest_.group_counts == {"MEANINGFUL_CHANGE": 1}
    commit = semantic_commit("a1", "add settings", digest_)
    assert build_semantic_evidence([commit]).verdict == "additive"


def test_digest_keeps_guardrail_violations():
    violation = NS(rule_id="protect-port", severity=NS(value="immutable"), file="svc.yaml",
                   semantic_path="config.port", message="Port is protected")
    digest_ = digest_file_diff(file_diff([], [], language="yaml", guardrails=[violation]),
                               path="svc.yaml", old_path="svc.yaml")
    assert digest_.guardrails == [{
        "rule_id": "protect-port",
        "severity": "immutable",
        "file": "svc.yaml",
        "semantic_path": "config.port",
        "message": "Port is protected",
    }]


# ---------------------------------------------------------------------------
# group-level evidence
# ---------------------------------------------------------------------------


def test_evidence_verdicts():
    style = semantic_commit("s1", "tidy", fs("a.py", group_counts={"IGNORED_STYLE": 2}, style_only=True))
    assert build_semantic_evidence([style]).verdict == "style-only"

    refactor = semantic_commit("r1", "move", fs("a.py", group_counts={"MOVED_CODE": 1, "REFACTORING": 1},
                                                has_semantic_changes=True))
    assert build_semantic_evidence([refactor]).verdict == "refactor-only"

    additive = semantic_commit("d1", "add", fs("a.py", group_counts={"MEANINGFUL_CHANGE": 1},
                                               meaningful_change_types={"ADDITION": 1}, has_semantic_changes=True,
                                               symbols=[SymbolChange(name="fresh", change="added", file="a.py")]))
    evidence = build_semantic_evidence([additive])
    assert evidence.verdict == "additive"
    assert evidence.added_symbols == ["fresh in a.py"]

    behavioral = semantic_commit("b1", "edit", fs("a.py", group_counts={"MEANINGFUL_CHANGE": 1},
                                                  meaningful_change_types={"MODIFICATION": 1},
                                                  has_semantic_changes=True))
    assert build_semantic_evidence([behavioral]).verdict == "behavioral"


def test_evidence_is_restricted_to_group_paths_and_reports_unreviewed_files():
    commit = Commit(sha="c1", message="mixed", parent_sha="p0")
    commit.semantics = digest(
        "c1",
        fs("api/a.py", group_counts={"MEANINGFUL_CHANGE": 1}, meaningful_change_types={"MODIFICATION": 1}),
        fs("web/b.ts", group_counts={"IGNORED_STYLE": 1}, style_only=True, language="typescript"),
        unreviewed=[UnreviewedFile(path="web/c.weird", kind="no_parser", reason="no parser")],
    )
    web = build_semantic_evidence([commit], paths=["web/b.ts", "web/c.weird"])
    assert web.files_reviewed == 1
    assert web.verdict == "style-only"
    assert web.complete is False
    assert web.unreviewed == [{"path": "web/c.weird", "kind": "no_parser", "reason": "no parser"}]
    api = build_semantic_evidence([commit], paths=["api/a.py"])
    assert api.verdict == "behavioral"
    assert api.complete is True
    assert api.languages == ["python"]


def test_symbols_added_then_removed_inside_a_group_cancel_out():
    first = semantic_commit("c1", "add", fs("a.py", group_counts={"MEANINGFUL_CHANGE": 1},
                                            symbols=[SymbolChange(name="tmp", change="added", file="a.py")]))
    second = semantic_commit("c2", "drop", fs("a.py", group_counts={"MEANINGFUL_CHANGE": 1},
                                              symbols=[SymbolChange(name="tmp", change="removed", file="a.py")]))
    evidence = build_semantic_evidence([first, second])
    assert evidence.added_symbols == []
    assert evidence.removed_symbols == []


def test_api_break_candidates_ignore_private_and_test_symbols():
    commit = semantic_commit(
        "c1",
        "change",
        fs("src/api.py", group_counts={"MEANINGFUL_CHANGE": 3}, symbols=[
            SymbolChange(name="public_fn", change="removed", file="src/api.py"),
            SymbolChange(name="_private", change="removed", file="src/api.py"),
            SymbolChange(name="handler", change="signature", file="src/api.py"),
        ]),
        fs("tests/test_api.py", group_counts={"MEANINGFUL_CHANGE": 1}, symbols=[
            SymbolChange(name="test_thing", change="removed", file="tests/test_api.py"),
        ]),
    )
    candidates = build_semantic_evidence([commit]).api_break_candidates
    assert candidates == ["public_fn in src/api.py", "handler in src/api.py"]


def test_no_semantics_means_no_evidence():
    assert build_semantic_evidence([Commit(sha="x", message="m")]) is None


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


def classify(*commits: Commit):
    group_ = ChangeGroup(pr=None, commits=list(commits), files=[f for c in commits for f in c.files])
    return Classifier().classify(group_, "base", "head")[0]


def test_style_only_diff_fallback_is_internal():
    item = classify(semantic_commit("s1", "tidy things", fs("src/app.py", group_counts={"IGNORED_STYLE": 2},
                                                           style_only=True)))
    assert (item.classification.kind, item.classification.audience, item.classification.semver) == (
        "internal", "internal", "none")
    assert "semantic:style-only" in item.evidence.commit_signals
    assert "semantic-classified" in item.evidence.commit_signals
    assert item.evidence.diff_summary["engine"] == "intentumdiff"
    assert item.evidence.diff_summary["semantic"]["verdict"] == "style-only"
    assert item.content.summary.startswith("IntentumDiff semantic review: formatting/style-only")


def test_refactor_only_diff_fallback_is_refactoring():
    item = classify(semantic_commit("r1", "reorganise", fs("src/app.py", group_counts={"MOVED_CODE": 2},
                                                          has_semantic_changes=True)))
    assert item.classification.kind == "refactoring"
    assert item.confidence >= 0.82


def test_additive_symbols_classify_as_added():
    item = classify(semantic_commit(
        "a1",
        "work",
        fs("src/app.py", group_counts={"MEANINGFUL_CHANGE": 1}, meaningful_change_types={"ADDITION": 1},
           has_semantic_changes=True, symbols=[SymbolChange(name="export_csv", change="added", file="src/app.py")]),
    ))
    assert (item.classification.kind, item.classification.semver) == ("added", "minor")
    assert "added: export_csv" in item.content.summary


def test_authored_conventional_commit_keeps_precedence_but_conflicts_are_flagged():
    item = classify(semantic_commit("f1", "feat: add export", fs("src/app.py", group_counts={"IGNORED_STYLE": 1},
                                                                  style_only=True)))
    assert item.classification.kind == "added"
    assert "semantic:conflict" in item.evidence.commit_signals
    assert item.confidence <= 0.7
    assert item.status == "needs-review"


def test_api_break_candidates_force_review():
    item = classify(semantic_commit(
        "b1",
        "fix: tidy api",
        fs("src/api.py", group_counts={"MEANINGFUL_CHANGE": 1}, meaningful_change_types={"DELETION": 1},
           symbols=[SymbolChange(name="create_user", change="removed", file="src/api.py")]),
    ))
    assert item.classification.kind == "fixed"
    assert "semantic:api-break-candidate" in item.evidence.commit_signals
    assert item.status == "needs-review"


def test_guardrails_force_review():
    item = classify(semantic_commit(
        "g1",
        "chore: bump",
        fs("deploy.yaml", language="yaml", group_counts={"MEANINGFUL_CHANGE": 1},
           guardrails=[{"rule_id": "protect-replicas", "severity": "immutable", "file": "deploy.yaml"}]),
    ))
    assert "semantic:guardrail" in item.evidence.commit_signals
    assert item.confidence <= 0.79
    assert "guardrails: protect-replicas (immutable)" in item.content.summary


def test_commit_intent_evidence_includes_semantic_review():
    commit = semantic_commit("c1", "stuff", fs("src/app.py", group_counts={"MEANINGFUL_CHANGE": 1},
                                               symbols=[SymbolChange(name="fresh", change="added", file="src/app.py")]))
    payload = commit_diff_intent_evidence(commit)
    assert payload["semantic_review"]["added_symbols"] == ["fresh"]
    plain = Commit(sha="c2", message="stuff", parent_sha="p0")
    assert "semantic_review" not in commit_diff_intent_evidence(plain)


# ---------------------------------------------------------------------------
# engine orchestration, caching
# ---------------------------------------------------------------------------


class FakeCommitDiffer:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def iter_file_diffs(self, repo_path, old_ref, new_ref):
        self.calls.append((old_ref, new_ref))
        yield NS(
            file_diff=file_diff([change("ADDITION", new=node("fresh", params=0))],
                                [group("MEANINGFUL_CHANGE", [0])]),
            old_path="a.py",
            new_path="a.py",
        )
        yield NS(old_path="b.bin", new_path="b.bin", reason="no parser for .bin", kind="no_parser")

    def finalize_commit_diff(self, results, errors, *, old_ref, new_ref):  # pragma: no cover - cross_file off
        raise AssertionError("cross-file analysis is opt-in")


def test_engine_annotates_and_caches_commit_digests(tmp_path):
    cache_path = tmp_path / "cache.json"
    fake = FakeCommitDiffer()
    engine = IntentumDiffEngine(".", cache=SemanticCache(cache_path), differ_factory=lambda: fake)
    commits = [Commit(sha="c1", message="m", parent_sha="p1"), Commit(sha="root", message="init")]
    progress: list[tuple[int, int]] = []
    engine.annotate(commits, progress=lambda current, total, sha: progress.append((current, total)))

    assert fake.calls == [("p1", "c1")]
    first = commits[0].semantics
    assert [f.path for f in first.files] == ["a.py"]
    assert first.unreviewed[0].kind == "no_parser"
    assert commits[1].semantics.unreviewed == []  # root commit had no files
    assert progress == [(1, 2), (2, 2)]
    assert cache_path.exists()

    # A fresh engine with the same cache never calls IntentumDiff again.
    again = [Commit(sha="c1", message="m", parent_sha="p1")]
    fresh = FakeCommitDiffer()
    IntentumDiffEngine(".", cache=SemanticCache(cache_path), differ_factory=lambda: fresh).annotate(again)
    assert fresh.calls == []
    assert again[0].semantics.files[0].symbols[0].name == "fresh"


def test_engine_degrades_one_commit_on_failure_without_caching(tmp_path):
    class Exploding(FakeCommitDiffer):
        def iter_file_diffs(self, repo_path, old_ref, new_ref):
            raise RuntimeError("boom")

    cache = SemanticCache(tmp_path / "cache.json")
    engine = IntentumDiffEngine(".", cache=cache, differ_factory=Exploding)
    commits = [Commit(sha="c1", message="m", parent_sha="p1")]
    engine.annotate(commits)
    assert commits[0].semantics.error == "RuntimeError: boom"
    assert cache.get(engine._cache_key("p1", "c1")) is None
    evidence = build_semantic_evidence(commits)
    assert evidence.complete is False
    assert evidence.errors == ["c1: RuntimeError: boom"]


def test_collector_attaches_semantics(monkeypatch):
    calls = []

    class RecordingEngine:
        def annotate(self, commits, progress=None):
            calls.append([c.sha for c in commits])
            for commit in commits:
                commit.semantics = digest(commit.sha)

    collector = Collector(NullPullRequestAdapter(), semantic_engine=RecordingEngine())
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kwargs: NS(stdout="b2\tb1\tsecond\tme\na1\t\tfirst\tme" if cmd[1] == "log" else "", returncode=0),
    )
    commits = collector.collect_commits("base", "head")
    assert calls == [["a1", "b2"]]
    assert all(isinstance(commit.semantics, CommitSemantics) for commit in commits)


# ---------------------------------------------------------------------------
# real IntentumDiff engine, end to end
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.mark.skipif(not HAS_INTENTUMDIFF, reason="intentumdiff-python is not installed")
def test_scan_is_powered_by_real_intentumdiff(monkeypatch, tmp_path: Path, capsys):
    from verlato.cli.main import main

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Tester")
    (repo / "app.py").write_text('def greet(name):\n    return "hi " + name\n\n\ndef gone(x):\n    return x\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    _git(repo, "tag", "v1")
    (repo / "app.py").write_text(
        'def greet(name, punct="!"):\n    return "hi " + name + punct\n\n\ndef fresh(y):\n    return y\n'
    )
    _git(repo, "commit", "-am", "update app")

    monkeypatch.chdir(repo)
    monkeypatch.setenv("VERLATO_MODEL_PROVIDER", "stub")
    monkeypatch.setenv("VERLATO_STATE_PATH", str(tmp_path / "state.json"))
    assert main(["scan", "--base", "v1", "--head", "HEAD"]) == 0
    items = json.loads(capsys.readouterr().out)
    assert len(items) == 1
    semantic = items[0]["evidence"]["diff_summary"]["semantic"]
    assert semantic["engine"] == "intentumdiff"
    assert semantic["verdict"] == "behavioral"
    assert "fresh in app.py" in semantic["added_symbols"]
    assert "gone in app.py" in semantic["removed_symbols"]
    assert "greet in app.py" in semantic["signature_changes"]
    assert "semantic:api-break-candidate" in items[0]["evidence"]["commit_signals"]
    assert items[0]["status"] == "needs-review"

    assert main(["--diff-engine", "git", "scan", "--base", "v1", "--head", "HEAD"]) == 0
    legacy = json.loads(capsys.readouterr().out)
    assert legacy[0]["evidence"]["diff_summary"]["engine"] == "git"
    assert legacy[0]["evidence"]["diff_summary"]["semantic"] is None
