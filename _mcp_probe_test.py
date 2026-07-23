import os

os.environ["APP_ENV"] = "test"
import asyncio
import traceback
from app.core.config import settings

print("ENV:", settings.ENVIRONMENT.value)
print("MCP_DEFAULT_TRANSPORT:", repr(getattr(settings, "MCP_DEFAULT_TRANSPORT", "<unset>")))
print("MCP_SERVER_URLS:", repr(getattr(settings, "MCP_SERVER_URLS", "<unset>")))
print("ENABLE_MCP_CLIENT:", getattr(settings, "ENABLE_MCP_CLIENT", "<unset>"))
from langchain_mcp_adapters.client import MultiServerMCPClient


async def main():
    client = MultiServerMCPClient({"mcp_server": {"url": "http://localhost:8002/mcp", "transport": "streamable_http"}})
    try:
        tools = await client.get_tools()
        print("TOOLS_OK:", [t.name for t in tools])
    except BaseException as e:
        print("EXC_TYPE:", type(e).__name__)
        print("EXC_MSG:", str(e))
        if hasattr(e, "exceptions"):
            for i, sub in enumerate(e.exceptions):
                print(f"--- SUB[{i}]:", type(sub).__name__, "::", str(sub))
                traceback.print_exception(type(sub), sub, sub.__traceback__)
        else:
            traceback.print_exc()


asyncio.run(main())
