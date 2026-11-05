"""Export the actual registered MCP surface without connecting to a phone."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import __version__
from src.server import mcp


async def export(path):
    tools = await mcp.list_tools()
    resources = await mcp.list_resources()
    prompts = await mcp.list_prompts()
    data = {"server": "android-root-mcp", "version": __version__, "tool_count": len(tools),
            "tools": [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools],
            "resources": [item.model_dump(mode="json", by_alias=True, exclude_none=True) for item in resources],
            "prompts": [item.model_dump(mode="json", by_alias=True, exclude_none=True) for item in prompts]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"path": str(path.resolve()), "tools": len(tools)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "docs/tool-schema.json")
    asyncio.run(export(parser.parse_args().output))
