import asyncio

import jsonschema
import pytest

from src import server


def test_tool_registration_schemas():
    tools = asyncio.run(server.mcp.list_tools())
    assert len(tools) == 53
    assert len({t.name for t in tools}) == len(tools)
    for tool in tools:
        jsonschema.Draft202012Validator.check_schema(tool.inputSchema)


def test_resource_and_prompt():
    assert str(asyncio.run(server.mcp.list_resources())[0].uri) == "android://capabilities"
    assert asyncio.run(server.mcp.list_prompts())[0].name == "android_development_task"


def test_catalog_without_device():
    content, structured = asyncio.run(server.mcp.call_tool("tool_catalog", {}))
    assert structured["version"] == "2.0.0"


def test_unknown_workflow_rejected_by_mcp_before_handler(monkeypatch):
    called = []
    monkeypatch.setattr(server.controller, "run_workflow", lambda *a: called.append(a))
    with pytest.raises(Exception, match="action|Input"):
        asyncio.run(server.mcp.call_tool("run_workflow", {"workflow": {"steps": [{"action": "typox"}]}}))
    assert not called
