import asyncio
import os
import sys
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_real_stdio_protocol_without_device(tmp_path):
    async def run():
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1",
                   ANDROID_MCP_STATE_DIR=str(tmp_path / "state"), ANDROID_MCP_LOG_FILE="")
        params = StdioServerParameters(command=sys.executable,
                                       args=[str(Path(__file__).parents[1]/"src/server.py")], env=env)
        with anyio.fail_after(30):
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    listing = await session.list_tools()
                    assert len(listing.tools) == 53
                    catalog = await session.call_tool("tool_catalog", {})
                    assert not catalog.isError and catalog.structuredContent["version"] == "2.0.0"
                    workflow = await session.call_tool("run_workflow", {"workflow": {"steps": [{"action": "sleep", "ms": 0}], "return_ui": False}})
                    assert not workflow.isError and workflow.structuredContent["success"]
                    invalid = await session.call_tool("run_workflow", {"workflow": {"steps": [{"action": "not_a_valid_action"}]}})
                    assert invalid.isError
                    pending = asyncio.create_task(session.call_tool("run_workflow", {"workflow": {
                        "steps": [{"action": "sleep", "ms": 5000}], "return_ui": False,
                    }}))
                    for _ in range(50):
                        state = await session.call_tool("workflow_control", {})
                        if state.structuredContent["active"]:
                            break
                        await asyncio.sleep(0.02)
                    cancelled = await session.call_tool("workflow_control", {"action": "cancel"})
                    assert cancelled.structuredContent["success"]
                    stopped = await pending
                    assert stopped.structuredContent["code"] == "CANCELLED"
                    resources = await session.list_resources()
                    assert str(resources.resources[0].uri) == "android://capabilities"
    asyncio.run(run())
