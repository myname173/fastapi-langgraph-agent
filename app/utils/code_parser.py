"""Shared code-parsing utilities for code_index, knowledge_graph, and smart_search.

Eliminates duplicated file-traversal and AST-parsing boilerplate across the
three services (L-12 fix). Previously each service had its own copy of:

- Workspace file traversal (skip hidden dirs, __pycache__, filter by extension)
- ``ast.parse`` + ``SyntaxError`` handling

The service-specific extraction logic (functions/classes/imports vs. entity
relations) stays in each service — only the shared infrastructure is here.
"""

import ast
from pathlib import Path
from typing import Any, List, Optional, Set

from app.core.logging import logger

# Default directories excluded from workspace traversal.
_DEFAULT_EXCLUDE_DIRS: Set[str] = {
    "__pycache__",
    ".git",
    ".venv",
    "venv",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}


def collect_workspace_files(
    workspace: Path,
    extensions: Optional[Set[str]] = None,
    exclude_dirs: Optional[Set[str]] = None,
) -> List[Path]:
    """Collect all code files in a workspace, skipping hidden dirs and caches.

    Consolidates the traversal logic that was duplicated in
    ``code_index_service._collect_files``,
    ``knowledge_graph_service.build_graph``, and
    ``smart_search_service._keyword_search`` (L-12 fix).

    Args:
        workspace: Root directory to traverse. Must exist.
        extensions: Set of allowed extensions (without the dot), e.g.
            ``{"py", "js"}``. If ``None``, all file types are returned.
        exclude_dirs: Set of directory names to skip. Defaults to common
            cache/VCS directories (``__pycache__``, ``.git``, ``.venv``,
            ``node_modules``, ...).

    Returns:
        List of absolute Path objects for matching files. Callers that
        need relative paths should call ``path.relative_to(workspace)``.
    """
    if not workspace.exists():
        return []

    excludes = exclude_dirs or _DEFAULT_EXCLUDE_DIRS
    files: List[Path] = []

    for p in workspace.rglob("*"):
        if not p.is_file():
            continue
        # Skip hidden directories (any path component starting with ".")
        # and configured cache/VCS directories.
        if any(part.startswith(".") or part in excludes for part in p.parts):
            continue
        if extensions is not None:
            if p.suffix.lstrip(".") not in extensions:
                continue
        files.append(p)

    return files


def safe_ast_parse(content: str, filename: str = "<unknown>") -> Optional[ast.AST]:
    """Parse Python source into an AST, returning None on failure.

    Consolidates the ``try: ast.parse(content) except: return None`` pattern
    duplicated in ``code_index_service._parse_python_structure`` and
    ``knowledge_graph_service._extract_relations`` (L-12 fix).

    Args:
        content: Python source code string.
        filename: Filename for error reporting (passed to ``ast.parse``).

    Returns:
        The parsed AST module, or ``None`` if the source has a ``SyntaxError``
        or other parsing failure. The failure is logged at ``debug`` level
        (expected for non-Python files misidentified by extension).
    """
    try:
        return ast.parse(content, filename=filename)
    except SyntaxError as e:
        logger.debug("ast_parse_syntax_error", filename=filename, error=str(e))
        return None
    except Exception as e:
        logger.debug("ast_parse_failed", filename=filename, error=str(e))
        return None


def extract_module_structure(tree: ast.AST) -> dict[str, Any]:
    """Extract functions, classes, and imports from a parsed Python AST.

    Consolidates the structural extraction shared between
    ``code_index_service._parse_python_structure`` and (partially)
    ``knowledge_graph_service._extract_relations`` (L-12 fix).

    Args:
        tree: A parsed AST (from :func:`safe_ast_parse` or ``ast.parse``).

    Returns:
        Dict with keys ``functions``, ``classes``, ``imports`` (each a list),
        and ``success`` (bool, always True when this function is called —
        callers should check the ``tree`` argument for None first).
    """
    functions: List[dict] = []
    classes: List[dict] = []
    imports: List[str] = []

    # Build a parent-class lookup so we can attribute methods to classes
    # without the O(n^2) nested ast.walk that the original used.
    method_to_class: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    method_to_class[id(item)] = node.name

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start_line = node.lineno
            end_line = getattr(node, "end_lineno", start_line + 1)
            functions.append(
                {
                    "name": node.name,
                    "start_line": start_line,
                    "end_line": end_line,
                    "class": method_to_class.get(id(node)),
                }
            )

        elif isinstance(node, ast.ClassDef):
            start_line = node.lineno
            end_line = getattr(node, "end_lineno", start_line + 1)
            classes.append(
                {
                    "name": node.name,
                    "start_line": start_line,
                    "end_line": end_line,
                }
            )

        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)

        elif isinstance(node, ast.ImportFrom):
            module = node.module if node.module else ""
            for alias in node.names:
                imports.append(f"{module}.{alias.name}" if module else alias.name)

    return {
        "functions": functions,
        "classes": classes,
        "imports": list(set(imports)),
        "success": True,
    }
