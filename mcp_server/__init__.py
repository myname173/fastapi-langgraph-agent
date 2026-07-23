"""In-repo MCP server — read-only git context + sqlite query for SDLC agents.

Exposes git introspection (log/show/branch/status/diff) and a read-only
sqlite query interface over SDLC task history + eval golden sets via the
Model Context Protocol. Designed to run as a CPU-only sidecar container
alongside the FastAPI app — zero GPU cost, zero node runtime.
"""

__version__ = "0.1.0"
