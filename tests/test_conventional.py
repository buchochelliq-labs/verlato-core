from releasepilot.core.conventional import parse_conventional_commit


def test_parse_feat():
    parsed = parse_conventional_commit("feat(api): add export endpoint")
    assert parsed is not None
    assert parsed.type == "feat"
    assert parsed.scope == "api"
    assert parsed.breaking is False


def test_parse_breaking():
    parsed = parse_conventional_commit("feat!: replace old auth\n\nBREAKING CHANGE: legacy token removed")
    assert parsed is not None
    assert parsed.breaking is True
