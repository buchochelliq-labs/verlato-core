#!/usr/bin/env bash
# Create the "taskboard" demo repository the documentation screenshots are taken from: a tiny
# Python project with a tagged release, one merged pull request, a fix, a refactoring, a
# vaguely named behavioural commit, a formatting-only commit and a docs commit. Author, dates
# and contents are fixed, so the commit IDs are the same on every run.
#
# Usage: scripts/screenshots/make_demo_repo.sh DIR   (DIR/taskboard is replaced)
set -euo pipefail

parent="${1:?usage: make_demo_repo.sh DIR}"
mkdir -p "$parent"
repo="$(cd "$parent" && pwd)/taskboard"
rm -rf "$repo"
mkdir -p "$repo"
cd "$repo"

export GIT_AUTHOR_NAME="Demo Developer" GIT_AUTHOR_EMAIL="dev@example.com"
export GIT_COMMITTER_NAME="Demo Developer" GIT_COMMITTER_EMAIL="dev@example.com"
at() { export GIT_AUTHOR_DATE="2026-10-$1T10:00:00Z" GIT_COMMITTER_DATE="2026-10-$1T10:00:00Z"; }
commit() { at "$1"; git add -A; git -c commit.gpgsign=false commit -q -m "$2"; }

git init -q -b main
git config tag.gpgSign false
mkdir -p src/taskboard tests
cat > src/taskboard/__init__.py <<'EOF'
"""Taskboard: a tiny task tracker."""
EOF
cat > src/taskboard/tasks.py <<'EOF'
from dataclasses import dataclass, field


@dataclass
class Task:
    title: str
    done: bool = False
    tags: list[str] = field(default_factory=list)


class Board:
    def __init__(self):
        self.tasks: list[Task] = []

    def add(self, title, tags=None):
        task = Task(title=title, tags=tags or [])
        self.tasks.append(task)
        return task

    def complete(self, title):
        for task in self.tasks:
            if task.title == title:
                task.done = True
                return task
        raise KeyError(title)

    def open_tasks(self):
        return [t for t in self.tasks if not t.done]
EOF
printf '# Taskboard\n\nA tiny task tracker.\n' > README.md
commit 01 "Initial taskboard"
git tag -a v1.0.0 -m "v1.0.0"

git switch -q -c feature/csv-export
cat > src/taskboard/export.py <<'EOF'
import csv
import io

from .tasks import Board


def export_csv(board: Board) -> str:
    """Return the board's tasks as CSV with a header row."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["title", "done", "tags"])
    for task in board.tasks:
        writer.writerow([task.title, "yes" if task.done else "no", ";".join(task.tags)])
    return out.getvalue()
EOF
commit 03 "add exporter"
cat > tests/test_export.py <<'EOF'
from taskboard.export import export_csv
from taskboard.tasks import Board


def test_export_has_header():
    board = Board()
    board.add("Write docs", tags=["docs"])
    assert export_csv(board).splitlines()[0] == "title,done,tags"
EOF
commit 04 "tests"
git switch -q main
at 05
git -c commit.gpgsign=false merge -q --no-ff feature/csv-export \
  -m "Merge pull request #12 from demo/feature/csv-export" -m "Export the board as CSV"

python3 - <<'EOF'
p = "src/taskboard/tasks.py"
t = open(p).read()
t = t.replace(
    "    def add(self, title, tags=None):\n        task = Task(title=title, tags=tags or [])",
    "    def add(self, title, tags=None):\n        title = title.strip()\n        if not title:\n"
    "            raise ValueError(\"A task needs a title\")\n        task = Task(title=title, tags=tags or [])",
)
open(p, "w").write(t)
EOF
commit 07 "fix: reject tasks with empty titles"
sed -i 's/def open_tasks(self):/def pending(self):/' src/taskboard/tasks.py
commit 09 "refactor(tasks): rename open_tasks to pending"
python3 - <<'EOF'
p = "src/taskboard/tasks.py"
t = open(p).read()
t = t.replace("from dataclasses import dataclass, field\n", "from dataclasses import dataclass, field\nfrom datetime import date\n")
t = t.replace(
    "    tags: list[str] = field(default_factory=list)\n",
    "    tags: list[str] = field(default_factory=list)\n    due: date | None = None\n",
)
t += "\n    def overdue(self, today):\n        return [t for t in self.pending() if t.due and t.due < today]\n"
open(p, "w").write(t)
EOF
commit 11 "wip"
printf '"""Taskboard: a tiny task tracker."""\n\n' > src/taskboard/__init__.py
commit 12 "style: tidy module docstring"
printf '\n## Export\n\nUse `export_csv(board)` to get the board as CSV.\n' >> README.md
commit 13 "docs: document CSV export"

# Verlato's configuration and state stay out of the demo's history.
cat > verlato.json <<'EOF'
{
  "provider": {"name": "mock"},
  "targets": {
    "changelog": {"path": "CHANGELOG.md", "format": "changelog"},
    "github-release": {"path": ".github/release.md", "format": "github-release"}
  }
}
EOF
printf '.verlato_*\nverlato.json\n' >> .git/info/exclude
echo "$repo"
