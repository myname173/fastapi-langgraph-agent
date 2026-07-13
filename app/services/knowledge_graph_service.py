"""Knowledge Graph Service for code dependency analysis.

Builds and queries a lightweight knowledge graph of code entities
(File, Class, Function) and their relationships (imports, calls, inherits, contains).
"""

import ast
import hashlib
import json
from pathlib import Path
from typing import Dict, List, Optional, Any, Set, Tuple

from app.core.config import settings
from app.core.logging import logger
from app.utils.code_parser import collect_workspace_files, safe_ast_parse


class KnowledgeGraphService:
    """Service for building and querying code dependency graphs.

    Uses a simple dict-based adjacency list representation for nodes and edges.
    Persists to JSON files per thread_id for isolation.
    """

    def __init__(self):
        """Initialize the knowledge graph service."""
        self._graphs: Dict[str, Dict[str, Any]] = {}
        self._file_hashes: Dict[str, Dict[str, str]] = {}

    def _get_graph(self, thread_id: str) -> Dict[str, Any]:
        """Get or create the graph for a thread."""
        if thread_id not in self._graphs:
            self._graphs[thread_id] = {
                "nodes": {},
                "edges": [],
                "node_by_name": {},
            }
        return self._graphs[thread_id]

    def _file_hash(self, content: str) -> str:
        """Compute SHA256 hash of content."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _get_persist_path(self, thread_id: str) -> Path:
        """Get the persist path for a thread's graph."""
        persist_dir = Path(settings.LOG_DIR) / "knowledge_graphs"
        persist_dir.mkdir(parents=True, exist_ok=True)
        return persist_dir / f"{thread_id}.json"

    async def load_graph(self, thread_id: str) -> bool:
        """Load a previously persisted graph from disk."""
        if not settings.ENABLE_KNOWLEDGE_GRAPH:
            return False

        try:
            persist_path = self._get_persist_path(thread_id)
            if persist_path.exists():
                with open(persist_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._graphs[thread_id] = data
                    if "file_hashes" in data:
                        self._file_hashes[thread_id] = data["file_hashes"]
                    logger.info(
                        "knowledge_graph_loaded",
                        thread_id=thread_id,
                        node_count=len(data.get("nodes", {})),
                        edge_count=len(data.get("edges", [])),
                    )
                    return True
        except Exception as e:
            logger.warning(
                "knowledge_graph_load_failed",
                thread_id=thread_id,
                error=str(e),
            )
        return False

    async def save_graph(self, thread_id: str) -> bool:
        """Persist the graph to disk."""
        if not settings.ENABLE_KNOWLEDGE_GRAPH:
            return False

        try:
            persist_path = self._get_persist_path(thread_id)
            graph = self._get_graph(thread_id)
            data = {
                **graph,
                "file_hashes": self._file_hashes.get(thread_id, {}),
            }
            with open(persist_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            logger.info(
                "knowledge_graph_saved",
                thread_id=thread_id,
                node_count=len(graph.get("nodes", {})),
                edge_count=len(graph.get("edges", [])),
            )
            return True
        except Exception as e:
            logger.warning(
                "knowledge_graph_save_failed",
                thread_id=thread_id,
                error=str(e),
            )
            return False

    def _resolve_import_to_file(self, workspace_path: str, import_name: str) -> Optional[str]:
        """Resolve an import path to a file path.

        Args:
            workspace_path: Absolute path to workspace
            import_name: Import path like 'app.core.config'

        Returns:
            Relative file path like 'app/core/config.py' or None
        """
        wp = Path(workspace_path)
        parts = import_name.split(".")

        for i in range(len(parts), 0, -1):
            module_parts = parts[:i]
            module_path = wp / "/".join(module_parts)

            if (module_path / "__init__.py").exists():
                return "/".join(module_parts) + "/__init__.py"
            if (module_path.with_suffix(".py")).exists():
                return "/".join(module_parts) + ".py"

        return None

    def _extract_relations(self, file_path: str, content: str, workspace_path: str) -> Dict[str, Any]:
        """Extract entities and relations from a Python file using ast.NodeVisitor.

        Args:
            file_path: Relative file path
            content: File content
            workspace_path: Absolute workspace path

        Returns:
            Dict with nodes and edges
        """
        nodes = []
        edges = []

        if not file_path.endswith(".py"):
            nodes.append(
                {
                    "id": f"file:{file_path}",
                    "type": "file",
                    "name": file_path,
                    "language": file_path.split(".")[-1] if "." in file_path else "unknown",
                }
            )
            return {"nodes": nodes, "edges": edges}

        tree = safe_ast_parse(content, filename=file_path)
        if tree is None:
            nodes.append(
                {
                    "id": f"file:{file_path}",
                    "type": "file",
                    "name": file_path,
                    "language": "py",
                }
            )
            return {"nodes": nodes, "edges": edges}

        nodes.append(
            {
                "id": f"file:{file_path}",
                "type": "file",
                "name": file_path,
                "language": "py",
            }
        )

        current_scope = []

        class RelationVisitor(ast.NodeVisitor):
            def __init__(self):
                self.imports: List[Tuple[str, str]] = []
                self.calls: List[Tuple[str, str]] = []
                self.classes: List[Tuple[str, List[str], int]] = []
                self.functions: List[Tuple[str, Optional[str], int]] = []
                self.current_class: Optional[str] = None

            def visit_Import(self, node: ast.Import):
                for alias in node.names:
                    resolved_file = self._resolve_import_to_file(workspace_path, alias.name)
                    if resolved_file:
                        self.imports.append((alias.name, resolved_file))
                self.generic_visit(node)

            def visit_ImportFrom(self, node: ast.ImportFrom):
                module = node.module if node.module else ""
                for alias in node.names:
                    full_name = f"{module}.{alias.name}" if module else alias.name
                    resolved_file = self._resolve_import_to_file(workspace_path, full_name)
                    if not resolved_file and module:
                        resolved_file = self._resolve_import_to_file(workspace_path, module)
                    if resolved_file:
                        self.imports.append((full_name, resolved_file))
                self.generic_visit(node)

            def visit_Call(self, node: ast.Call):
                func_name = ""
                if isinstance(node.func, ast.Name):
                    func_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    func_name = self._get_attr_name(node.func)

                if func_name:
                    caller = ".".join(reversed(current_scope)) if current_scope else file_path
                    self.calls.append((func_name, caller))
                self.generic_visit(node)

            def visit_ClassDef(self, node: ast.ClassDef):
                self.current_class = node.name
                bases = []
                for base in node.bases:
                    if isinstance(base, ast.Name):
                        bases.append(base.id)
                    elif isinstance(base, ast.Attribute):
                        bases.append(self._get_attr_name(base))
                self.classes.append((node.name, bases, node.lineno))
                current_scope.append(node.name)
                self.generic_visit(node)
                current_scope.pop()
                self.current_class = None

            def visit_FunctionDef(self, node: ast.FunctionDef):
                parent_class = self.current_class
                self.functions.append((node.name, parent_class, node.lineno))
                current_scope.append(node.name)
                self.generic_visit(node)
                current_scope.pop()

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
                parent_class = self.current_class
                self.functions.append((node.name, parent_class, node.lineno))
                current_scope.append(node.name)
                self.generic_visit(node)
                current_scope.pop()

            def _get_attr_name(self, node: ast.Attribute) -> str:
                parts = []
                current = node
                while isinstance(current, ast.Attribute):
                    parts.append(current.attr)
                    current = current.value
                if isinstance(current, ast.Name):
                    parts.append(current.id)
                return ".".join(reversed(parts))

            _resolve_import_to_file = self._resolve_import_to_file

        visitor = RelationVisitor()
        visitor.visit(tree)

        for cls_name, bases, _ in visitor.classes:
            class_id = f"class:{file_path}:{cls_name}"
            nodes.append(
                {
                    "id": class_id,
                    "type": "class",
                    "name": cls_name,
                    "file": file_path,
                }
            )

            edges.append(
                {
                    "from": f"file:{file_path}",
                    "to": class_id,
                    "relation": "contains",
                }
            )

            for base in bases:
                edges.append(
                    {
                        "from": class_id,
                        "to": f"class:{base}",
                        "relation": "inherits",
                    }
                )

        for func_name, parent_class, _ in visitor.functions:
            if parent_class:
                func_id = f"func:{file_path}:{parent_class}:{func_name}"
                edges.append(
                    {
                        "from": f"class:{file_path}:{parent_class}",
                        "to": func_id,
                        "relation": "contains",
                    }
                )
            else:
                func_id = f"func:{file_path}:{func_name}"
                edges.append(
                    {
                        "from": f"file:{file_path}",
                        "to": func_id,
                        "relation": "contains",
                    }
                )

            nodes.append(
                {
                    "id": func_id,
                    "type": "function",
                    "name": func_name,
                    "class": parent_class,
                    "file": file_path,
                }
            )

        for import_name, resolved_file in visitor.imports:
            edges.append(
                {
                    "from": f"file:{file_path}",
                    "to": f"file:{resolved_file}",
                    "relation": "imports",
                    "import_name": import_name,
                }
            )

        for func_name, caller in visitor.calls:
            if "." in caller:
                caller_parts = caller.split(".")
                if len(caller_parts) >= 2:
                    caller_id = f"func:{file_path}:{caller_parts[-2]}:{caller_parts[-1]}"
                else:
                    caller_id = f"func:{file_path}:{caller}"
            else:
                caller_id = f"func:{file_path}:{caller}"

            edges.append(
                {
                    "from": caller_id,
                    "to": f"func:{func_name}",
                    "relation": "calls",
                }
            )

        return {"nodes": nodes, "edges": edges}

    def _add_nodes(self, graph: Dict[str, Any], nodes: List[Dict[str, Any]]):
        """Add nodes to the graph."""
        for node in nodes:
            graph["nodes"][node["id"]] = node
            key = f"{node['type']}:{node['name']}"
            if key not in graph["node_by_name"]:
                graph["node_by_name"][key] = []
            graph["node_by_name"][key].append(node["id"])

    def _add_edges(self, graph: Dict[str, Any], edges: List[Dict[str, Any]]):
        """Add edges to the graph."""
        for edge in edges:
            edge_key = f"{edge['from']}->{edge['to']}->{edge['relation']}"
            if edge_key not in {f"{e['from']}->{e['to']}->{e['relation']}" for e in graph["edges"]}:
                graph["edges"].append(edge)

    def _remove_file_nodes(self, graph: Dict[str, Any], file_path: str):
        """Remove all nodes and edges related to a file."""
        file_prefix = f"file:{file_path}"
        class_prefix = f"class:{file_path}:"
        func_prefix = f"func:{file_path}:"

        to_remove = []
        for node_id in graph["nodes"]:
            if node_id.startswith(file_prefix) or node_id.startswith(class_prefix) or node_id.startswith(func_prefix):
                to_remove.append(node_id)

        for node_id in to_remove:
            del graph["nodes"][node_id]

        graph["edges"] = [
            e
            for e in graph["edges"]
            if not (
                e["from"].startswith(file_prefix)
                or e["from"].startswith(class_prefix)
                or e["from"].startswith(func_prefix)
                or e["to"].startswith(file_prefix)
                or e["to"].startswith(class_prefix)
                or e["to"].startswith(func_prefix)
            )
        ]

        new_node_by_name = {}
        for key, ids in graph["node_by_name"].items():
            filtered = [nid for nid in ids if nid not in to_remove]
            if filtered:
                new_node_by_name[key] = filtered
        graph["node_by_name"] = new_node_by_name

    async def build_graph(self, workspace_path: str, thread_id: str) -> dict:
        """Build or rebuild the knowledge graph for a workspace.

        Args:
            workspace_path: Absolute path to workspace
            thread_id: Thread ID for isolation

        Returns:
            Stats dict with node_count, edge_count, file_count
        """
        if not settings.ENABLE_KNOWLEDGE_GRAPH:
            return {"node_count": 0, "edge_count": 0, "file_count": 0, "enabled": False}

        wp = Path(workspace_path)
        if not wp.is_dir():
            raise ValueError(f"Workspace path is not a directory: {workspace_path}")

        graph = self._get_graph(thread_id)
        graph["nodes"] = {}
        graph["edges"] = []
        graph["node_by_name"] = {}

        if thread_id not in self._file_hashes:
            self._file_hashes[thread_id] = {}

        file_count = 0
        skipped = 0

        for file_path in collect_workspace_files(wp, extensions={"py"}):
            rel_path = str(file_path.relative_to(wp))

            try:
                content = file_path.read_text(encoding="utf-8")

                if len(content.encode("utf-8")) > settings.CODE_INDEX_MAX_FILE_SIZE:
                    skipped += 1
                    continue

                relations = self._extract_relations(rel_path, content, workspace_path)
                self._add_nodes(graph, relations["nodes"])
                self._add_edges(graph, relations["edges"])

                self._file_hashes[thread_id][rel_path] = self._file_hash(content)
                file_count += 1

            except UnicodeDecodeError:
                skipped += 1
            except Exception as e:
                logger.warning(
                    "knowledge_graph_file_failed",
                    file=str(file_path),
                    error=str(e),
                )
                skipped += 1

        await self.save_graph(thread_id)

        logger.info(
            "knowledge_graph_build_complete",
            thread_id=thread_id,
            node_count=len(graph["nodes"]),
            edge_count=len(graph["edges"]),
            file_count=file_count,
            skipped=skipped,
        )

        return {
            "node_count": len(graph["nodes"]),
            "edge_count": len(graph["edges"]),
            "file_count": file_count,
            "skipped": skipped,
            "enabled": True,
        }

    async def update_file_graph(self, workspace_path: str, thread_id: str, file_path: str) -> dict:
        """Incrementally update the graph for a single file.

        Args:
            workspace_path: Absolute path to workspace
            thread_id: Thread ID for isolation
            file_path: Relative path of the file to update

        Returns:
            Stats dict with node_count, edge_count
        """
        if not settings.ENABLE_KNOWLEDGE_GRAPH:
            return {"node_count": 0, "edge_count": 0, "enabled": False}

        wp = Path(workspace_path)
        abs_path = wp / file_path

        if not abs_path.is_file():
            raise ValueError(f"File not found: {file_path}")

        try:
            content = abs_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return {"node_count": 0, "edge_count": 0, "error": "unicode_decode_error"}

        if len(content.encode("utf-8")) > settings.CODE_INDEX_MAX_FILE_SIZE:
            return {"node_count": 0, "edge_count": 0, "error": "file_too_large"}

        content_hash = self._file_hash(content)

        if thread_id in self._file_hashes and self._file_hashes[thread_id].get(file_path) == content_hash:
            return {"node_count": 0, "edge_count": 0, "unchanged": True}

        graph = self._get_graph(thread_id)
        self._remove_file_nodes(graph, file_path)

        relations = self._extract_relations(file_path, content, workspace_path)
        self._add_nodes(graph, relations["nodes"])
        self._add_edges(graph, relations["edges"])

        if thread_id not in self._file_hashes:
            self._file_hashes[thread_id] = {}
        self._file_hashes[thread_id][file_path] = content_hash

        await self.save_graph(thread_id)

        logger.info(
            "knowledge_graph_file_updated",
            thread_id=thread_id,
            file=file_path,
            node_count=len(graph["nodes"]),
            edge_count=len(graph["edges"]),
        )

        return {
            "node_count": len(graph["nodes"]),
            "edge_count": len(graph["edges"]),
            "file_path": file_path,
        }

    def find_impact(self, thread_id: str, entity_name: str, max_depth: Optional[int] = None) -> List[Dict[str, Any]]:
        """Find all entities that would be impacted by modifying an entity.

        This does a reverse traversal from the entity to find all callers.

        Args:
            thread_id: Thread ID for isolation
            entity_name: Name of the entity (function, class, or file)
            max_depth: Maximum traversal depth (default: KNOWLEDGE_GRAPH_MAX_DEPTH)

        Returns:
            List of impact paths with entities and relations
        """
        if not settings.ENABLE_KNOWLEDGE_GRAPH:
            return []

        depth = max_depth or settings.KNOWLEDGE_GRAPH_MAX_DEPTH
        graph = self._get_graph(thread_id)

        start_nodes = []
        for key, ids in graph["node_by_name"].items():
            if key.endswith(f":{entity_name}"):
                start_nodes.extend(ids)

        if not start_nodes:
            for node_id in graph["nodes"]:
                node = graph["nodes"][node_id]
                if node.get("name") == entity_name:
                    start_nodes.append(node_id)

        if not start_nodes:
            return []

        visited: Set[str] = set()
        impact_paths: List[Dict[str, Any]] = []

        def dfs(node_id: str, current_path: List[Dict[str, Any]], current_depth: int):
            if current_depth > depth or node_id in visited:
                return

            visited.add(node_id)

            for edge in graph["edges"]:
                if edge["to"] == node_id:
                    caller_id = edge["from"]
                    caller_node = graph["nodes"].get(caller_id, {})

                    new_path = current_path + [
                        {
                            "entity": caller_id,
                            "type": caller_node.get("type", "unknown"),
                            "name": caller_node.get("name", "unknown"),
                            "file": caller_node.get("file", "unknown"),
                            "relation": edge["relation"],
                        }
                    ]

                    impact_paths.append(
                        {
                            "path": new_path,
                            "depth": current_depth,
                        }
                    )

                    dfs(caller_id, new_path, current_depth + 1)

        for start_node in start_nodes:
            dfs(start_node, [], 1)

        return sorted(impact_paths, key=lambda x: x["depth"])

    def get_dependency_chain(
        self, thread_id: str, entry_point: str, max_depth: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Get the dependency chain from an entry point.

        This does a forward traversal to find all entities that the entry point depends on.

        Args:
            thread_id: Thread ID for isolation
            entry_point: Name of the entry point (function, class, or file)
            max_depth: Maximum traversal depth (default: KNOWLEDGE_GRAPH_MAX_DEPTH)

        Returns:
            List of dependency paths with entities and relations
        """
        if not settings.ENABLE_KNOWLEDGE_GRAPH:
            return []

        depth = max_depth or settings.KNOWLEDGE_GRAPH_MAX_DEPTH
        graph = self._get_graph(thread_id)

        start_nodes = []
        for key, ids in graph["node_by_name"].items():
            if key.endswith(f":{entry_point}"):
                start_nodes.extend(ids)

        if not start_nodes:
            for node_id in graph["nodes"]:
                node = graph["nodes"][node_id]
                if node.get("name") == entry_point:
                    start_nodes.append(node_id)

        if not start_nodes:
            return []

        visited: Set[str] = set()
        dependency_paths: List[Dict[str, Any]] = []

        def dfs(node_id: str, current_path: List[Dict[str, Any]], current_depth: int):
            if current_depth > depth or node_id in visited:
                return

            visited.add(node_id)

            for edge in graph["edges"]:
                if edge["from"] == node_id:
                    dep_id = edge["to"]
                    dep_node = graph["nodes"].get(dep_id, {})

                    new_path = current_path + [
                        {
                            "entity": dep_id,
                            "type": dep_node.get("type", "unknown"),
                            "name": dep_node.get("name", "unknown"),
                            "file": dep_node.get("file", "unknown"),
                            "relation": edge["relation"],
                        }
                    ]

                    dependency_paths.append(
                        {
                            "path": new_path,
                            "depth": current_depth,
                        }
                    )

                    dfs(dep_id, new_path, current_depth + 1)

        for start_node in start_nodes:
            dfs(start_node, [], 1)

        return sorted(dependency_paths, key=lambda x: x["depth"])

    def get_graph_summary(self, thread_id: str) -> dict:
        """Get a summary of the graph.

        Args:
            thread_id: Thread ID for isolation

        Returns:
            Summary dict with counts by type
        """
        if not settings.ENABLE_KNOWLEDGE_GRAPH:
            return {"enabled": False}

        graph = self._get_graph(thread_id)
        nodes = graph.get("nodes", {})

        counts = {"file": 0, "class": 0, "function": 0}
        for node in nodes.values():
            node_type = node.get("type", "unknown")
            if node_type in counts:
                counts[node_type] += 1

        relation_counts = {}
        for edge in graph.get("edges", []):
            rel = edge.get("relation", "unknown")
            relation_counts[rel] = relation_counts.get(rel, 0) + 1

        return {
            "node_count": len(nodes),
            "edge_count": len(graph.get("edges", [])),
            "nodes_by_type": counts,
            "relations_by_type": relation_counts,
            "enabled": True,
        }

    async def cleanup_graph(self, thread_id: str) -> bool:
        """Clean up all persisted graph data for a thread.

        Removes the JSON file and in-memory graph data.

        Args:
            thread_id: Thread ID whose graph should be cleaned up

        Returns:
            True if cleanup succeeded, False otherwise
        """
        if not settings.ENABLE_KNOWLEDGE_GRAPH:
            return False

        cleaned = False

        try:
            persist_path = self._get_persist_path(thread_id)
            if persist_path.exists():
                persist_path.unlink()
                cleaned = True

            if thread_id in self._graphs:
                del self._graphs[thread_id]
                cleaned = True

            if thread_id in self._file_hashes:
                del self._file_hashes[thread_id]
                cleaned = True

            if cleaned:
                logger.info("knowledge_graph_cleaned_up", thread_id=thread_id)
            return True
        except Exception as e:
            logger.warning(
                "knowledge_graph_cleanup_failed",
                thread_id=thread_id,
                error=str(e),
            )
            return False


knowledge_graph_service = KnowledgeGraphService()
