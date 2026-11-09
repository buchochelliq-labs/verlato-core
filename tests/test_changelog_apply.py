from verlato.core.drafts import DraftService


def test_reapplying_a_version_replaces_its_subsections():
    existing = (
        "# Changelog\n\n### v2 (2026-01-01)\n\n### Fixed\n- Old fix\n\n"
        "### v1 (2025-12-01)\n\n### Added\n- First\n"
    )
    rendered = "### v2 (2026-01-02)\n\n### Added\n- New\n"
    updated, action = DraftService()._apply_changelog(existing, rendered, "v2")
    assert action == "replace"
    assert "Old fix" not in updated
    assert updated == (
        "# Changelog\n\n### v2 (2026-01-02)\n\n### Added\n- New\n\n"
        "### v1 (2025-12-01)\n\n### Added\n- First\n"
    )
