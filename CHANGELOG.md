### Unreleased (2026-05-06)

### Added
- feat: implement prioritized backlog items: Mock summary: Write one concise release-note sentence based primarily on t
- feat: add VS Code extension for ReleasePilot with draft management and preview functionality: Validates a seamless development experience by integrating Verlato into VS Code, providing immediate access to draft handling and content previews.
- feat: add suggestion feature for release items and enhance release control view: changed files: modified .releasepilot_state.json, modified .vscode/settings.json, modified ARCHITECTURE.md, modified BACKLOG.md, modified README.md, and 48 more; statuses: added:12, modified:41; areas: backend(16), docs(5), general(26), tests(12); config: releasepilot/config.py, tests/test_config_and_drafts.py, .vscode/settings.json

### Changed
- Merge pull request #4 from n1ckyb/feature/implement-releasepilot-mvp-5bh9uu (PR #4): changed files: added ARCHITECTURE.md, added BACKLOG.md, modified README.md, added pyproject.toml, added releasepilot.sample.json, and 24 more; statuses: added:28, modified:1; areas: docs(3), general(22), tests(4); config: pyproject.toml
- Merge pull request #7 from n1ckyb/feature/find-next-items-in-backlog (PR #7): changed files: modified BACKLOG.md, modified releasepilot/core/grouper.py, added tests/test_grouper_non_pr.py; statuses: added:1, modified:2; areas: docs(1), general(1), tests(1)
- Merge pull request #8 from n1ckyb/feature/review-backlog-items (PR #8): changed files: modified BACKLOG.md, modified releasepilot/cli/main.py, modified releasepilot/core/classifier.py, modified releasepilot/core/generator.py, modified releasepilot/core/grouper.py, and 5 more; statuses: added:2, modified:8; areas: docs(1), general(4), tests(5)
- Merge pull request #9 from n1ckyb/feature/find-next-item-on-the-list (PR #9): changed files: modified BACKLOG.md, modified README.md, modified releasepilot/cli/main.py, modified releasepilot/core/collector.py, modified tests/test_cli_integration_temp_repo.py, and 1 more; statuses: modified:6; areas: docs(2), general(2), tests(2)
- Merge pull request #10 from n1ckyb/feature/create-detailed-plan-for-next-items [general] (PR #10): changed files: modified pyproject.toml, added releasepilot/core/checksum.py, modified releasepilot/core/classifier.py, modified releasepilot/core/collector.py, modified releasepilot/core/generator.py, and 11 more; statuses: added:4, modified:12; areas: general(16); config: pyproject.toml
- Merge pull request #10 from n1ckyb/feature/create-detailed-plan-for-next-items [infra] (PR #10): changed files: added .github/workflows/ci.yml; statuses: added:1; areas: infra(1); config: .github/workflows/ci.yml
- Add Ollama provider support and enhance GitHub Copilot tests: changed files: modified .gitignore, modified .releasepilot_state.json, modified README.md, modified releasepilot.sample.json, modified releasepilot/cli/main.py, and 19 more; statuses: added:2, modified:22; areas: backend(5), docs(2), general(13), tests(4); config: releasepilot/config.py, tests/test_config_and_drafts.py

### Internal
- Merge pull request #5 from n1ckyb/feature/add-release-provenance-tracking-feature (PR #5): changed files: modified BACKLOG.md; statuses: modified:1; areas: docs(1)
- docs: add architecture and backlog planning docs: changed files: added ARCHITECTURE.md, added BACKLOG.md, modified README.md, added pyproject.toml, added releasepilot.sample.json, and 24 more; statuses: added:28, modified:1; areas: docs(3), general(22), tests(4); config: pyproject.toml
- Add backlog item for release lineage by file area: changed files: modified BACKLOG.md; statuses: modified:1; areas: docs(1)
