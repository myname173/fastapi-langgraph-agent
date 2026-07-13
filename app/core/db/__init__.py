"""Database infrastructure package.

Hosts the unified async PostgreSQL connection pool and async session
factory shared by LangGraph's ``AsyncPostgresSaver`` and the business
DB layer (when ``ENABLE_ASYNC_DB`` is enabled).
"""
