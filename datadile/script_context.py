import ast
import re
from pathlib import Path


DEFAULT_CONTEXT_CHAR_LIMIT = 50_000
_SQL_START_RE = re.compile(r"^\s*(select|with|insert|update|delete|create|alter|drop)\b", re.I)


def detect_script_language(script: str, script_path: str | Path | None = None) -> str:
    if script_path is not None:
        suffix = Path(script_path).suffix.lower()
        if suffix == ".py":
            return "python"
        if suffix == ".sql":
            return "sql"

    try:
        ast.parse(script)
    except SyntaxError:
        if _SQL_START_RE.search(script):
            return "sql"
        return "unknown"

    return "python"


def expand_python_internal_dependencies(
    script: str,
    script_path: str | Path,
    char_limit: int = DEFAULT_CONTEXT_CHAR_LIMIT,
) -> str:
    """Append local Python dependencies imported by script, recursively, within char_limit."""
    root_path = Path(script_path).resolve()
    dependency_blocks = []
    remaining = max(0, char_limit - len(script))

    for dependency_path in _iter_internal_dependencies(script, root_path):
        if remaining <= 0:
            break

        try:
            dependency_code = dependency_path.read_text()
        except OSError:
            continue

        block = f"\n\n# Internal dependency: {dependency_path}\n{dependency_code}"
        if len(block) > remaining:
            block = block[:remaining]
        dependency_blocks.append(block)
        remaining -= len(block)

    return script + "".join(dependency_blocks)


def build_api_code(
    script: str,
    script_path: str | Path,
    char_limit: int = DEFAULT_CONTEXT_CHAR_LIMIT,
) -> str:
    if detect_script_language(script, script_path) != "python":
        return script
    return expand_python_internal_dependencies(script, script_path, char_limit)


def _iter_internal_dependencies(script: str, script_path: Path):
    seen = {script_path}
    queued = list(_resolve_imports(script, script_path))

    while queued:
        dependency_path = queued.pop(0)
        if dependency_path in seen:
            continue
        seen.add(dependency_path)
        yield dependency_path

        try:
            dependency_code = dependency_path.read_text()
        except OSError:
            continue
        queued.extend(_resolve_imports(dependency_code, dependency_path))


def _resolve_imports(script: str, source_path: Path) -> list[Path]:
    try:
        tree = ast.parse(script)
    except SyntaxError:
        return []

    dependencies = []
    seen = set()
    for import_node in ast.walk(tree):
        candidates = []
        if isinstance(import_node, ast.Import):
            for alias in import_node.names:
                candidates.extend(_resolve_absolute_module(alias.name, source_path))
        elif isinstance(import_node, ast.ImportFrom):
            candidates.extend(_resolve_import_from(import_node, source_path))

        for candidate in candidates:
            if candidate == source_path or candidate in seen:
                continue
            seen.add(candidate)
            dependencies.append(candidate)

    return dependencies


def _resolve_import_from(import_node: ast.ImportFrom, source_path: Path) -> list[Path]:
    module_parts = import_node.module.split(".") if import_node.module else []
    if import_node.level:
        base_dir = _relative_import_base(source_path, import_node.level)
        paths = _module_candidates(base_dir, module_parts)
        for alias in import_node.names:
            if alias.name != "*":
                paths.extend(_module_candidates(base_dir, [*module_parts, alias.name]))
        return _existing_python_files(paths)

    paths = []
    if import_node.module:
        paths.extend(_resolve_absolute_module(import_node.module, source_path))
        for alias in import_node.names:
            if alias.name != "*":
                paths.extend(_resolve_absolute_module(f"{import_node.module}.{alias.name}", source_path))
    return paths


def _resolve_absolute_module(module_name: str, source_path: Path) -> list[Path]:
    module_parts = module_name.split(".")
    candidates = []
    for root in _candidate_roots(source_path):
        candidates.extend(_module_candidates(root, module_parts))
    return _existing_python_files(candidates)


def _candidate_roots(source_path: Path) -> list[Path]:
    roots = [source_path.parent, Path.cwd().resolve()]
    return list(dict.fromkeys(roots))


def _relative_import_base(source_path: Path, level: int) -> Path:
    base = source_path.parent
    for _ in range(max(0, level - 1)):
        base = base.parent
    return base


def _module_candidates(root: Path, module_parts: list[str]) -> list[Path]:
    if not module_parts:
        return [root / "__init__.py"]

    module_path = root.joinpath(*module_parts)
    return [module_path.with_suffix(".py"), module_path / "__init__.py"]


def _existing_python_files(paths: list[Path]) -> list[Path]:
    existing = []
    seen = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen or not resolved.is_file():
            continue
        if any(part in {"site-packages", "dist-packages", ".venv", "venv"} for part in resolved.parts):
            continue
        seen.add(resolved)
        existing.append(resolved)
    return existing
