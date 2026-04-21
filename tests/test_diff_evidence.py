from releasepilot.core.diff_evidence import DiffEvidenceBuilder
from releasepilot.core.models import ChangedFile


def test_diff_evidence_builder_summarizes_and_flags():
    files = [
        ChangedFile(path="api/public/users.py", patch="@@ -1 +1 @@\n-old\n+new"),
        ChangedFile(path="migrations/2026_add_users.py", patch="@@ -1 +1 @@\n-create\n+create2"),
        ChangedFile(path="config/settings.yaml", patch="@@ -1 +1 @@\n-a\n+b"),
    ]
    evidence = DiffEvidenceBuilder().build(files)
    assert "api/public/users.py" in evidence.api_changes
    assert evidence.migration_changes == ["migrations/2026_add_users.py"]
    assert evidence.config_changes == ["config/settings.yaml"]
    assert evidence.docs_only is False
    assert evidence.tests_only is False
    assert "api/public/users.py" in evidence.representative_hunks
