from releasepilot.core.classifier import Classifier
from releasepilot.core.models import ChangedFile, ChangeGroup, Commit, PullRequest
from releasepilot.providers.base import ProviderRequest, ProviderResponse, ReleasePilotProvider


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


def test_provider_summary_prompt_includes_diff_hunks_and_files():
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
    assert "use polished release-note wording" in provider.summary_request.prompt
    assert "src/search.py" in provider.summary_request.prompt
    assert "+return ranked_results" in provider.summary_request.prompt
    assert provider.summary_request.evidence["representative_hunks"]["src/search.py"]


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
    assert result.budget["files_included"] == 12
    assert result.budget["files_total"] == 14
    assert result.budget["files_skipped"] == 2
    assert result.budget["commits_included"] == 3
    assert result.budget["commits_total"] == 4
    assert result.budget["commits_skipped"] == 1
    assert result.budget["pr_body_chars_included"] <= 503


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


class _CapturingProvider(ReleasePilotProvider):
    def __init__(self, summary: str) -> None:
        self.summary = summary
        self.summary_request: ProviderRequest | None = None

    def summarize(self, request: ProviderRequest) -> ProviderResponse:
        self.summary_request = request
        return ProviderResponse(content=self.summary, confidence=0.8, model="capture")

    def classify(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(content="", confidence=0.0, model="capture")
