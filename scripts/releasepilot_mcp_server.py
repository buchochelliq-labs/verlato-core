from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from releasepilot.mcp.server import create_fastmcp_server


mcp = create_fastmcp_server()

if __name__ == "__main__":
    mcp.run()
