from verlato.core.classifier import Classifier
from verlato.core.models import ChangedFile, ChangeGroup, Commit, CommitIntent, PullRequest
from verlato.providers.base import ProviderRequest, ProviderResponse, VerlatoProvider


def test_classifier_prefers_label_based_area():
    group = ChangeGroup(
        pr=PullRequest(
            number=5,
            title="Backend fix",
            labels=["backend"],
            files=[ChangedFile(path="frontend/app.tsx")],
        ),
        commits=[Commit(sha="abc", message="fix: correct query")],
    )
    item = Classifier().classify(group, base_ref="v1.0.0", head_ref="main")[0]
    assert item.classification.area == "backend"


def test_classifier_marks_internal_by_label():
    group = ChangeGroup(
        pr=PullRequest(
            number=6,
            title="Maintenance",
            labels=["chore"],
            files=[ChangedFile(path="src/app.py")],
        ),
        commits=[Commit(sha="def", message="chore: clean comments")],
    )
    item = Classifier().classify(group, base_ref="v1.0.0", head_ref="main")[0]
    assert item.classification.audience == "internal"


def test_classifier_keeps_breaking_changes_as_their_own_changelog_kind():
    group = ChangeGroup(
        pr=PullRequest(
            number=7,
            title="Replace public auth API",
            files=[ChangedFile(path="api/auth.py", status="modified", patch="+new auth contract")],
        ),
        commits=[Commit(sha="abc", message="feat!: replace public auth API")],
    )

    item = Classifier().classify(group, base_ref="v1.0.0", head_ref="main")[0]

    assert item.classification.kind == "breaking"
    assert item.classification.semver == "major"


def test_classifier_detects_breaking_change_footer_case_insensitively():
    group = ChangeGroup(
        pr=PullRequest(
            number=7,
            title="Rename configuration flags",
            files=[ChangedFile(path="src/config.py", status="modified", patch="+new flag names")],
        ),
        commits=[
            Commit(
                sha="abc",
                message=(
                    "refactor: rename configuration flags\n\n"
                    "Breaking-Change: old CI flag names are no longer accepted"
                ),
            )
        ],
    )

    item = Classifier().classify(group, base_ref="v1.0.0", head_ref="main")[0]

    assert item.classification.kind == "breaking"
    assert item.classification.semver == "major"


def test_classifier_detects_common_changelog_parts_from_conventional_types():
    cases = [
        ("docs: document auth setup", "docs/auth.md", "documentation"),
        ("perf: speed up search", "src/search.py", "performance"),
        ("refactor: split release renderer", "src/render.py", "refactoring"),
        ("test: cover release renderer", "tests/test_render.py", "tests"),
        ("build: update packaging", "pyproject.toml", "build"),
    ]

    for message, path, expected_kind in cases:
        group = ChangeGroup(
            pr=PullRequest(
                number=8,
                title=message,
                files=[ChangedFile(path=path, status="modified", patch="+change")],
            ),
            commits=[Commit(sha=f"sha-{expected_kind}", message=message)],
        )

        item = Classifier().classify(group, base_ref="v1.0.0", head_ref="main")[0]

        assert item.classification.kind == expected_kind


def test_classifier_uses_inferred_conventional_intent():
    group = ChangeGroup(
        pr=PullRequest(
            number=8,
            title="Wire token endpoint",
            files=[ChangedFile(path="api/tokens.py", status="modified", patch="+token endpoint")],
        ),
        commits=[
            Commit(
                sha="inf",
                message="wire token endpoint",
                inferred_intent=CommitIntent(
                    type="feat",
                    scope="api",
                    description="add token endpoint",
                    confidence=0.82,
                    model="mock",
                ),
            )
        ],
    )

    item = Classifier().classify(group, base_ref="v1.0.0", head_ref="main")[0]

    assert item.classification.kind == "added"
    assert item.classification.semver == "minor"
    assert "inferred-conventional:feat(api)" in item.evidence.commit_signals


def test_classifier_maps_inferred_breaking_intent_to_major():
    group = ChangeGroup(
        pr=PullRequest(
            number=8,
            title="Replace token endpoint",
            files=[ChangedFile(path="api/tokens.py", status="modified", patch="+new token endpoint")],
        ),
        commits=[
            Commit(
                sha="inf",
                message="replace token endpoint",
                inferred_intent=CommitIntent(
                    type="feat",
                    scope="api",
                    description="replace token endpoint",
                    breaking=True,
                    confidence=0.88,
                    model="mock",
                ),
            )
        ],
    )

    item = Classifier().classify(group, base_ref="v1.0.0", head_ref="main")[0]

    assert item.classification.kind == "breaking"
    assert item.classification.semver == "major"
    assert "inferred-breaking" in item.evidence.commit_signals


def test_classifier_detects_changelog_parts_from_labels_and_statuses():
    deprecated_group = ChangeGroup(
        pr=PullRequest(
            number=9,
            title="Start deprecating legacy tokens",
            labels=["deprecated"],
            files=[ChangedFile(path="api/tokens.py", status="modified", patch="+legacy warning")],
        ),
        commits=[Commit(sha="dep", message="update tokens")],
    )
    added_group = ChangeGroup(
        pr=PullRequest(number=10, title="Add helper", files=[ChangedFile(path="src/new_helper.py", status="added")]),
        commits=[Commit(sha="add", message="add helper")],
    )
    removed_group = ChangeGroup(
        pr=PullRequest(
            number=11,
            title="Remove helper",
            files=[ChangedFile(path="src/old_helper.py", status="removed")],
        ),
        commits=[Commit(sha="rm", message="remove helper")],
    )

    classifier = Classifier()

    deprecated = classifier.classify(deprecated_group, base_ref="v1.0.0", head_ref="main")[0]
    assert classifier.classify(added_group, base_ref="v1.0.0", head_ref="main")[0].classification.kind == "added"
    assert classifier.classify(removed_group, base_ref="v1.0.0", head_ref="main")[0].classification.kind == "removed"
    assert deprecated.classification.kind == "deprecated"


def test_classifier_uses_pr_diff_for_summary():
    group = ChangeGroup(
        pr=PullRequest(
            number=7,
            title="Improve search",
            files=[
                ChangedFile(
                    path="src/search.py",
                    status="modified",
                    patch="@@ -1 +1 @@\n-return []\n+return ranked_results",
                )
            ],
        ),
        commits=[Commit(sha="abc", message="update search")],
    )

    item = Classifier().classify(group, base_ref="v1.0.0", head_ref="main")[0]

    assert "src/search.py" in item.content.summary
    assert item.content.summary != "Improve search"
    assert item.evidence.diff_summary["representative_hunks"]["src/search.py"]


def test_classifier_uses_commit_diff_for_non_pr_summary():
    group = ChangeGroup(
        pr=None,
        commits=[Commit(sha="abc", message="update search")],
        files=[
            ChangedFile(
                path="src/search.py",
                status="modified",
                patch="@@ -1 +1 @@\n-return []\n+return ranked_results",
            )
        ],
    )

    item = Classifier().classify(group, base_ref="v1.0.0", head_ref="main")[0]

    assert item.content.summary != "Derived from commit history"
    assert "src/search.py" in item.content.summary
    assert item.evidence.files == ["src/search.py"]


def test_provider_summary_prompt_uses_patch_free_change_summary_by_default():
    provider = _CapturingProvider("Search now ranks matching results")
    group = ChangeGroup(
        pr=PullRequest(
            number=8,
            title="Improve search",
            body="Ranks close matches higher.",
            files=[
                ChangedFile(
                    path="src/search.py",
                    status="modified",
                    patch="@@ -1 +1 @@\n-return []\n+return ranked_results",
                )
            ],
        ),
        commits=[Commit(sha="abc", message="update search")],
    )

    item = Classifier(provider=provider).classify(group, base_ref="v1.0.0", head_ref="main")[0]

    assert item.content.summary == "Search now ranks matching results"
    assert "Lead with what changed and why it matters" in provider.summary_request.prompt
    assert "renders the final release note as Markdown" in provider.summary_request.prompt
    assert "use polished release-note wording" in provider.summary_request.prompt
    assert "src/search.py" in provider.summary_request.prompt
    assert "+return ranked_results" not in provider.summary_request.prompt
    assert "omitted by privacy mode" in provider.summary_request.prompt
    assert "representative_hunks" not in provider.summary_request.evidence
    change_summary = provider.summary_request.evidence["change_summary"]
    assert change_summary["files"][0]["path"] == "src/search.py"
    assert change_summary["files"][0]["patch_chars_available"] > 0
    assert change_summary["privacy"]["raw_patch_chars_sent"] == 0
    assert provider.summary_request.metadata["request"]["contains_raw_patch_markers"] is False


def test_provider_summary_prompt_supports_configurable_variants():
    group = ChangeGroup(
        pr=PullRequest(
            number=8,
            title="Improve search",
            labels=["internal"],
            files=[ChangedFile(path="tests/test_search.py", status="modified", patch="+assert ranked_results")],
        ),
        commits=[Commit(sha="abc", message="test: update search coverage")],
    )
    classifier = Classifier()
    diff = classifier.diff_builder.build(group.pr.files)

    prompt = classifier._build_summary_prompt(
        title="Improve search",
        labels=["internal"],
        pr_body="",
        diff=diff,
        commits=group.commits,
        suggestion_count=3,
        kind="internal",
        audience="internal",
    )

    assert "Return exactly 3 distinct complete release-note options as a numbered list" in prompt
    assert "downstream consumers understand the change" in prompt
    assert "Lead with what changed and why it matters" in prompt
    assert "Release context: kind=internal, audience=internal" in prompt
    assert "Prefer concrete active verbs" in prompt
    assert "internal, docs, tests, or maintenance changes, use concise commit-like wording" in prompt
    assert "Tone: polished release-note wording" in prompt


def test_provider_summary_prompt_includes_requested_tone():
    group = ChangeGroup(
        pr=PullRequest(
            number=7,
            title="Improve search",
            files=[ChangedFile(path="src/search.py", status="modified", patch="+rank API results")],
        ),
        commits=[Commit(sha="abc", message="feat: improve search ranking")],
    )
    classifier = Classifier()
    diff = classifier.diff_builder.build(group.pr.files)

    prompt = classifier._build_summary_prompt(
        title="Improve search",
        labels=[],
        pr_body="",
        diff=diff,
        commits=group.commits,
        tone="technical",
    )

    assert "Tone: precise engineering wording" in prompt


def test_provider_summary_prompt_includes_project_style_controls():
    group = ChangeGroup(
        pr=PullRequest(
            number=7,
            title="Improve release control",
            files=[ChangedFile(path="src/release_control.py", status="modified", patch="+review flow")],
        ),
        commits=[Commit(sha="abc", message="feat: improve draft review")],
    )
    classifier = Classifier()
    diff = classifier.diff_builder.build(group.pr.files)

    prompt = classifier._build_summary_prompt(
        title="Improve release control",
        labels=[],
        pr_body="",
        diff=diff,
        commits=group.commits,
        style_guidance="Prefer user workflow language.",
        project_vocabulary=["Release Control", "draft review"],
        banned_phrases=["stuff", "things"],
    )

    assert "Project style guidance: Prefer user workflow language." in prompt
    assert "Preferred project vocabulary: Release Control, draft review" in prompt
    assert "Avoid these phrases exactly: stuff, things" in prompt


def test_provider_summary_prompt_caps_changed_file_list():
    files = [
        ChangedFile(path=f"src/module_{index:02}.py", status="modified", patch="")
        for index in range(25)
    ]
    group = ChangeGroup(
        pr=PullRequest(number=8, title="Improve modules", files=files),
        commits=[Commit(sha="abc", message="update modules")],
    )
    classifier = Classifier()
    diff = classifier.diff_builder.build(group.pr.files)

    prompt = classifier._build_summary_prompt(
        title="Improve modules",
        labels=[],
        pr_body="",
        diff=diff,
        commits=group.commits,
    )

    assert "src/module_00.py" in prompt
    assert "src/module_11.py" in prompt
    assert "src/module_12.py" not in prompt
    assert "... and 13 more" in prompt


def test_provider_summary_prompt_truncates_large_context():
    long_body = "body " * 200
    long_summary_patch = "+changed behavior\n" * 60
    files = [
        ChangedFile(path="src/large.py", status="modified", patch=long_summary_patch),
        ChangedFile(path="src/other.py", status="modified", patch=long_summary_patch),
        ChangedFile(path="src/third.py", status="modified", patch=long_summary_patch),
        ChangedFile(path="src/fourth.py", status="modified", patch=long_summary_patch),
    ]
    group = ChangeGroup(
        pr=PullRequest(number=8, title="Improve modules", body=long_body, files=files),
        commits=[
            Commit(sha="a", message="update one"),
            Commit(sha="b", message="update two"),
            Commit(sha="c", message="update three"),
            Commit(sha="d", message="update four"),
        ],
    )
    classifier = Classifier()
    diff = classifier.diff_builder.build(group.pr.files)

    prompt = classifier._build_summary_prompt(
        title="Improve modules",
        labels=[],
        pr_body=group.pr.body,
        diff=diff,
        commits=group.commits,
    )

    assert "update one; update two; update three; ... and 1 more" in prompt
    assert "update four" not in prompt
    assert "src/fourth.py:" not in prompt
    assert len(prompt) < 4000


def test_provider_summary_prompt_reports_budget():
    files = [
        ChangedFile(path=f"src/module_{index:02}.py", status="modified", patch="+line")
        for index in range(14)
    ]
    group = ChangeGroup(
        pr=PullRequest(number=8, title="Improve modules", body="body " * 200, files=files),
        commits=[
            Commit(sha="a", message="update one"),
            Commit(sha="b", message="update two"),
            Commit(sha="c", message="update three"),
            Commit(sha="d", message="update four"),
        ],
    )
    classifier = Classifier()
    diff = classifier.diff_builder.build(group.pr.files)

    result = classifier._build_summary_prompt_with_budget(
        title="Improve modules",
        labels=[],
        pr_body=group.pr.body,
        diff=diff,
        commits=group.commits,
    )

    assert result.budget["prompt_chars"] == len(result.prompt)
    assert result.budget["profile"] == "summary"
    assert result.budget["files_included"] == 12
    assert result.budget["files_total"] == 14
    assert result.budget["files_skipped"] == 2
    assert result.budget["commits_included"] == 3
    assert result.budget["commits_total"] == 4
    assert result.budget["commits_skipped"] == 1
    assert result.budget["pr_body_chars_included"] <= 503


def test_suggestion_prompt_profile_uses_conservative_budget():
    files = [
        ChangedFile(
            path=f"src/module_{index:02}.py",
            status="modified",
            patch=(
                "@@ -1,2 +1,2 @@\n"
                f"-old behavior {index}\n"
                f"+new behavior {index}\n"
                "@@ -20,2 +20,2 @@\n"
                f"-old fallback {index}\n"
                f"+new fallback {index}\n"
            ),
        )
        for index in range(10)
    ]
    group = ChangeGroup(
        pr=PullRequest(number=8, title="Improve modules", body="body " * 200, files=files),
        commits=[
            Commit(sha="a", message="update one"),
            Commit(sha="b", message="update two"),
            Commit(sha="c", message="update three"),
            Commit(sha="d", message="update four"),
        ],
    )
    classifier = Classifier()
    diff = classifier.diff_builder.build(group.pr.files)

    result = classifier._build_summary_prompt_with_budget(
        title="Improve modules",
        labels=["backend"],
        pr_body=group.pr.body,
        diff=diff,
        commits=group.commits,
        suggestion_count=3,
        kind="changed",
        audience="external",
        tone="technical",
        style_guidance="Use precise product language.",
        project_vocabulary=["Release Control"],
        banned_phrases=["nice things"],
        prompt_profile="suggestion",
        include_hunks=True,
    )

    assert result.budget["prompt_chars"] == len(result.prompt)
    assert result.budget["profile"] == "suggestion-conservative"
    assert result.budget["files_included"] == 8
    assert result.budget["files_total"] == 10
    assert result.budget["files_skipped"] == 2
    assert result.budget["commits_included"] == 2
    assert result.budget["commits_total"] == 4
    assert result.budget["commits_skipped"] == 2
    assert result.budget["pr_body_chars_included"] <= 243
    assert result.budget["hunk_files_included"] == 2
    assert result.budget["hunk_files_total"] == 10
    assert result.budget["hunks_included"] == 2
    assert result.budget["hunks_total"] == 20
    assert result.budget["hunk_chars_included"] <= 320
    assert result.budget["hunk_chars_total"] >= result.budget["hunk_chars_included"]
    assert result.budget["hunk_chars_skipped"] > 0
    assert "Return exactly 3 distinct complete release-note options" in result.prompt
    assert "Tone: precise engineering wording" in result.prompt
    assert "Project style guidance: Use precise product language." in result.prompt
    assert "Preferred project vocabulary: Release Control" in result.prompt
    assert "Avoid these phrases exactly: nice things" in result.prompt
    assert "update one; update two; ... and 2 more" in result.prompt
    assert "update three" not in result.prompt
    assert "src/module_08.py" not in result.prompt


def test_suggestion_prompt_profiles_offer_lean_and_minimal_budgets():
    files = [
        ChangedFile(
            path=f"src/module_{index:02}.py",
            status="modified",
            patch=(
                "@@ -1,2 +1,2 @@\n"
                f"-old behavior {index}\n"
                f"+new behavior {index}\n"
            ),
        )
        for index in range(10)
    ]
    group = ChangeGroup(
        pr=PullRequest(number=8, title="Improve modules", body="body " * 200, files=files),
        commits=[
            Commit(sha="a", message="update one"),
            Commit(sha="b", message="update two"),
            Commit(sha="c", message="update three"),
            Commit(sha="d", message="update four"),
        ],
    )
    classifier = Classifier()
    diff = classifier.diff_builder.build(group.pr.files)

    conservative = classifier._build_summary_prompt_with_budget(
        title="Improve modules",
        labels=["backend"],
        pr_body=group.pr.body,
        diff=diff,
        commits=group.commits,
        prompt_profile="conservative",
    )
    lean = classifier._build_summary_prompt_with_budget(
        title="Improve modules",
        labels=["backend"],
        pr_body=group.pr.body,
        diff=diff,
        commits=group.commits,
        prompt_profile="lean",
    )
    minimal = classifier._build_summary_prompt_with_budget(
        title="Improve modules",
        labels=["backend"],
        pr_body=group.pr.body,
        diff=diff,
        commits=group.commits,
        prompt_profile="minimal",
    )

    assert lean.budget["profile"] == "suggestion-lean"
    assert minimal.budget["profile"] == "suggestion-minimal"
    assert conservative.budget["files_included"] == 8
    assert lean.budget["files_included"] == 5
    assert minimal.budget["files_included"] == 3
    assert lean.budget["prompt_chars"] < conservative.budget["prompt_chars"]
    assert minimal.budget["prompt_chars"] < lean.budget["prompt_chars"]
    assert minimal.budget["pr_body_chars_included"] <= lean.budget["pr_body_chars_included"]


def test_classifier_rejects_low_value_provider_summary():
    provider = _CapturingProvider("Improve search")
    group = ChangeGroup(
        pr=PullRequest(
            number=9,
            title="Improve search",
            files=[ChangedFile(path="src/search.py", status="modified", patch="@@ -1 +1 @@\n-old\n+new")],
        ),
        commits=[Commit(sha="abc", message="Improve search")],
    )

    item = Classifier(provider=provider).classify(group, base_ref="v1.0.0", head_ref="main")[0]

    assert item.content.summary != "Improve search"
    assert "provider-summary" not in " ".join(item.evidence.commit_signals)


def test_provider_classification_normalizes_changelog_kind_aliases():
    provider = _ClassifyingProvider("breaking-change/api/external/major")
    group = ChangeGroup(
        pr=PullRequest(
            number=12,
            title="Replace public auth API",
            files=[ChangedFile(path="api/auth.py", status="modified", patch="+new auth contract")],
        ),
        commits=[Commit(sha="abc", message="update auth")],
    )

    item = Classifier(provider=provider).classify(group, base_ref="v1.0.0", head_ref="main")[0]

    assert item.classification.kind == "breaking"
    assert item.classification.semver == "major"


class _CapturingProvider(VerlatoProvider):
    def __init__(self, summary: str) -> None:
        self.summary = summary
        self.summary_request: ProviderRequest | None = None

    def summarize(self, request: ProviderRequest) -> ProviderResponse:
        self.summary_request = request
        return ProviderResponse(content=self.summary, confidence=0.8, model="capture")

    def classify(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(content="", confidence=0.0, model="capture")


class _ClassifyingProvider(VerlatoProvider):
    def __init__(self, classification: str) -> None:
        self.classification = classification

    def summarize(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(
            content="Describes the change with enough concrete detail",
            confidence=0.8,
            model="capture",
        )

    def classify(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(content=self.classification, confidence=0.9, model="capture")
