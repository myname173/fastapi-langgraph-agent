"""This file contains the main application entry point."""

from contextlib import asynccontextmanager
from datetime import datetime

from dotenv import load_dotenv
from fastapi import (
    FastAPI,
    Request,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from asgi_correlation_id import CorrelationIdMiddleware

from app.api.v1.api import api_router
from app.api.v1.chatbot import agent
from app.core.cache import cache_service
from app.core.config import settings
from app.core.db.pool import (
    close_pool,
    init_pool,
)
from app.core.db.session import (
    close_async_engine,
    init_async_engine,
)
from app.core.limiter import limiter
from app.core.logging import logger
from app.core.metrics import setup_metrics
from app.core.middleware import (
    LoggingContextMiddleware,
    MetricsMiddleware,
    ProfilingMiddleware,
)
from app.core.observability import langfuse_init
from app.core.task_manager import task_manager
from app.services.database import database_service
from app.services.memory import memory_service

# Load environment variables
load_dotenv()
langfuse_init()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handle application startup and shutdown events."""
    logger.info(
        "application_startup",
        project_name=settings.PROJECT_NAME,
        version=settings.VERSION,
        api_prefix=settings.API_V1_STR,
    )

    # Initialize cache service (connects to Valkey if configured)
    try:
        await cache_service.initialize()
    except Exception as e:
        logger.exception("cache_initialization_failed", error=str(e))

    # Open the shared async PostgreSQL pool up-front. This pool backs
    # both the LangGraph ``AsyncPostgresSaver`` checkpointer and (when
    # ``ENABLE_ASYNC_DB`` is on) the business DB ``AsyncSession`` layer.
    try:
        await init_pool()
    except Exception as e:
        logger.exception("db_pool_init_failed", error=str(e))

    # When async DB is enabled, also bring up the SQLAlchemy 2.x async
    # engine for business DB operations. This is a separate pool from
    # the psycopg pool above (LangGraph), but both are async.
    if settings.ENABLE_ASYNC_DB:
        try:
            init_async_engine()
            logger.info("async_db_enabled")
        except Exception as e:
            logger.exception("async_engine_init_failed", error=str(e))

    # Pre-warm the LangGraph agent: create graph + connection pool at startup
    # to avoid cold-start latency on the first request
    try:
        await agent.create_graph()
        logger.info("graph_pre_warmed")
    except Exception as e:
        logger.exception("graph_pre_warm_failed", error=str(e))

    # Pre-warm mem0 AsyncMemory: initializes pgvector connection and schema check
    # so the first search() cache miss or add() doesn't pay the ~130ms cold-init cost
    try:
        await memory_service.initialize()
    except Exception as e:
        logger.exception("memory_service_pre_warm_failed", error=str(e))

    # MCP (Model Context Protocol) client: dynamically load tools from
    # remote MCP servers.  Opt-in via ENABLE_MCP_CLIENT + MCP_SERVER_URLS.
    # Non-fatal — missing packages or unreachable servers log warnings.
    if settings.ENABLE_MCP_CLIENT and settings.MCP_SERVER_URLS:
        try:
            from app.core.langgraph.tools import default_registry

            loaded = await default_registry.load_mcp_servers(settings.MCP_SERVER_URLS)
            logger.info(
                "mcp_client_loaded",
                server_count=len(settings.MCP_SERVER_URLS),
                tool_count=len(loaded),
                tools=loaded,
            )
        except Exception as e:
            logger.exception("mcp_client_init_failed", error=str(e))

    yield

    # Cleanup on shutdown — order matters:
    # 1. Stop accepting new background work (drain tracked tasks)
    await task_manager.shutdown()
    # 2. Close cache connections
    await cache_service.close()
    # 3. Dispose the async SQLAlchemy engine (business DB layer)
    if settings.ENABLE_ASYNC_DB:
        await close_async_engine()
    # 4. Close the shared async connection pool (LangGraph + business DB)
    await close_pool()
    logger.info("application_shutdown")


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description=settings.DESCRIPTION,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan,
)

# Set up Prometheus metrics
setup_metrics(app)

# Add logging context middleware (must be added before other middleware to capture context)
app.add_middleware(LoggingContextMiddleware)

# Add custom metrics middleware
app.add_middleware(MetricsMiddleware)

# Add profiling middleware (DEBUG only — saves HTML to /tmp on slow requests)
if settings.DEBUG:
    app.add_middleware(ProfilingMiddleware)

# Add correlation ID middleware — must be outermost so request_id is set before all others
app.add_middleware(CorrelationIdMiddleware)

# Set up rate limiter exception handler
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # pyright: ignore[reportArgumentType]


# Add validation exception handler
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handle validation errors from request data.

    Args:
        request: The request that caused the validation error
        exc: The validation error

    Returns:
        JSONResponse: A formatted error response
    """
    # Log the validation error
    logger.error(
        "validation_error",
        client_host=request.client.host if request.client else "unknown",
        path=request.url.path,
        errors=str(exc.errors()),
    )

    # Format the errors to be more user-friendly
    formatted_errors = []
    for error in exc.errors():
        loc = " -> ".join([str(loc_part) for loc_part in error["loc"] if loc_part != "body"])
        formatted_errors.append({"field": loc, "message": error["msg"]})

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Validation error", "errors": formatted_errors},
    )


# Set up CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API router
app.include_router(api_router, prefix=settings.API_V1_STR)

# Mount workspace as static files for artifact preview (HTML/games/dashboards).
# Gated by ENABLE_WORKSPACE_PREVIEW flag — when off, the mount and the asset
# tools (app/core/langgraph/tools/asset_tools.py) are both disabled, keeping
# pure-backend tasks unaffected. The mount is read-only; writes still go
# through write_file / download_asset with path traversal + extension checks.
if settings.ENABLE_WORKSPACE_PREVIEW:
    settings.SDLC_WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    app.mount(
        settings.WORKSPACE_PREVIEW_URL_PREFIX,
        StaticFiles(directory=settings.SDLC_WORKSPACE_ROOT),
        name="workspace",
    )
    logger.info(
        "workspace_preview_mounted",
        prefix=settings.WORKSPACE_PREVIEW_URL_PREFIX,
        root=str(settings.SDLC_WORKSPACE_ROOT),
    )


@app.get("/")
@limiter.limit(settings.RATE_LIMIT_ENDPOINTS["root"][0])
async def root(request: Request):
    """Root endpoint returning basic API information."""
    logger.info("root_endpoint_called")
    return {
        "name": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "status": "healthy",
        "environment": settings.ENVIRONMENT.value,
        "swagger_url": "/docs",
        "redoc_url": "/redoc",
    }


@app.get("/health")
@limiter.limit(settings.RATE_LIMIT_ENDPOINTS["health"][0])
async def health_check(request: Request) -> JSONResponse:
    """Health check endpoint with environment-specific information.

    Returns:
        JSONResponse: Health status payload, with HTTP 503 when the
        database is unreachable so load balancers can drop the instance.
    """
    logger.info("health_check_called")

    # Check database connectivity
    db_healthy = await database_service.health_check()

    response = {
        "status": "healthy" if db_healthy else "degraded",
        "version": settings.VERSION,
        "environment": settings.ENVIRONMENT.value,
        "components": {"api": "healthy", "database": "healthy" if db_healthy else "unhealthy"},
        "timestamp": datetime.now().isoformat(),
    }

    # If DB is unhealthy, set the appropriate status code
    status_code = status.HTTP_200_OK if db_healthy else status.HTTP_503_SERVICE_UNAVAILABLE

    return JSONResponse(content=response, status_code=status_code)
