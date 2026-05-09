from __future__ import annotations

from verlato.core.classifier import Classifier
from verlato.core.models import ChangedFile, ChangeGroup, Commit, CommitIntent, PullRequest
from verlato.core.suggestion_tools import SuggestionEvidenceToolProvider


def test_suggestion_evidence_tools_return_manifest_and_patch_chunks():
    patch = "@@ -1 +1 @@\n-old auth\n+new auth\n"
    group = ChangeGroup(
        pr=PullRequest(number=7, title="Improve auth"),
        commits=[Commit(sha="a1", message="feat: improve auth")],
        files=[ChangedFile(path="src/auth.py", status="modified", patch=patch)],
    )
    item = Classifier().classify(group, base_ref="v1.0.0", head_ref="HEAD")[0]

    provider = SuggestionEvidenceToolProvider.from_groups([group], [item])
    listed = provider.call_tool("verlato_list_suggestion_items", {"include_files": True})
    detail = provider.call_tool("verlato_get_suggestion_item", {"item_id": item.id})
    chunk = provider.call_tool(
        "verlato_get_file_patch",
        {"item_id": item.id, "path": "src/auth.py", "offset": 0, "limit": 12},
    )
    search = provider.call_tool("verlato_search_item_diff", {"item_id": item.id, "query": "new auth"})

    assert listed["items"][0]["item_id"] == item.id
    assert listed["items"][0]["files"][0]["patch_available"] is True
    assert detail["commits"][0]["subject"] == "feat: improve auth"
    assert chunk["patch"] == patch[:12]
    assert chunk["truncated"] is True
    assert search["results"][0]["path"] == "src/auth.py"


def test_suggestion_evidence_tools_include_inferred_commit_intent():
    group = ChangeGroup(
        pr=PullRequest(number=8, title="Wire auth API"),
        commits=[
            Commit(
                sha="a1",
                message="wire auth api",
                inferred_intent=CommitIntent(
                    type="feat",
                    scope="api",
                    description="add auth API",
                    confidence=0.86,
                    model="mock",
                    rationale="Touches API auth files.",
                ),
            )
        ],
        files=[ChangedFile(path="api/auth.py", status="modified", patch="+auth api")],
    )
    item = Classifier().classify(group, base_ref="v1.0.0", head_ref="HEAD")[0]

    provider = SuggestionEvidenceToolProvider.from_groups([group], [item])
    detail = provider.call_tool("verlato_get_suggestion_item", {"item_id": item.id})

    assert detail["commits"][0]["inferred_conventional"]["type"] == "feat"
    assert detail["commits"][0]["inferred_conventional"]["scope"] == "api"
    assert detail["commits"][0]["effective_conventional"]["source"] == "inferred"


def test_suggestion_evidence_tools_export_ollama_tool_schema():
    provider = SuggestionEvidenceToolProvider([])

    tools = provider.ollama_tools()

    assert tools
    assert tools[0]["type"] == "function"
    assert tools[0]["function"]["name"] == "verlato_list_suggestion_items"
    assert tools[0]["function"]["parameters"]["type"] == "object"
