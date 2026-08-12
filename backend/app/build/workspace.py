"""Where generated projects live on disk.

One directory per project under `WORKSPACE_ROOT`, shared with the build
container through a Docker volume: the backend writes source, the builder
compiles it, and neither reaches into the other's process.

Every path here can originate from a language model, so every path is
resolved and checked against the workspace root before anything touches the
filesystem. A generated file called `../../app/main.py` has to fail loudly,
not overwrite the API.
"""

import re
import shutil
from collections.abc import Iterable
from pathlib import Path

from app.core.config import settings
from app.orchestrator.contracts import SourceFile

# A project id, not a path.
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
# The builder container runs as an unprivileged user with a different uid, so
# it must still be able to create `build/` inside a directory we created.
DIR_MODE = 0o777
# Never handed to the compiler and never listed as project source.
BUILD_DIR = "build"


class WorkspaceError(ValueError):
    """A project id or path that must not reach the filesystem."""


def root() -> Path:
    """Read the setting on every call so tests can point it at a temp dir."""
    return Path(settings.workspace_root)


def workspace_path(project_id: str) -> Path:
    if not SAFE_ID.match(str(project_id or "")):
        raise WorkspaceError(f"invalid project id: {project_id!r}")
    return root() / str(project_id)


def safe_join(project_id: str, relative: str) -> Path:
    """Resolve a project-relative path, or refuse it."""
    text = str(relative or "").strip()
    if not text or Path(text).is_absolute() or "\x00" in text:
        raise WorkspaceError(f"unsafe path: {relative!r}")
    base = workspace_path(project_id)
    resolved_base = base.resolve()
    candidate = (base / text).resolve()
    if candidate == resolved_base or not candidate.is_relative_to(resolved_base):
        raise WorkspaceError(f"path escapes the workspace: {relative!r}")
    return candidate


def exists(project_id: str) -> bool:
    return workspace_path(project_id).is_dir()


def relax_permissions(project_id: str) -> Path:
    """Make every directory in the workspace writable by the build container.

    The backend runs as root and the builder runs as uid 1000, so a directory
    created here with default permissions is one the compiler cannot write
    into -- `mkdir -p build` fails with EACCES before gcc is ever called.
    Anything that creates or copies directories must end up here.
    """
    base = workspace_path(project_id)
    if not base.is_dir():
        return base
    base.chmod(DIR_MODE)
    for path in base.rglob("*"):
        if path.is_dir():
            path.chmod(DIR_MODE)
    return base


def ensure_workspace(project_id: str, *, clean: bool = False) -> Path:
    path = workspace_path(project_id)
    if clean and path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    # mkdir(mode=...) is filtered by the process umask; chmod is not.
    path.chmod(DIR_MODE)
    return path


def write_file(project_id: str, relative: str, contents: str) -> Path:
    target = safe_join(project_id, relative)  # refuses anything outside the workspace
    base = workspace_path(project_id).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    for parent in [target.parent, *target.parent.parents]:
        if not parent.is_relative_to(base):
            break
        parent.chmod(DIR_MODE)
    target.write_text(contents, encoding="utf-8")
    return target


def write_files(
    project_id: str,
    files: Iterable[SourceFile],
    *,
    clean: bool = False,
) -> list[str]:
    """Materialise a firmware bundle. Returns the paths written."""
    ensure_workspace(project_id, clean=clean)
    written: list[str] = []
    for source in files:
        write_file(project_id, source.path, source.contents)
        written.append(source.path)
    relax_permissions(project_id)
    return written


def copy_tree(source: Path, project_id: str, *, clean: bool = True) -> Path:
    """Copy a directory into a workspace (templates, the golden project).

    `copytree` finishes with `copystat` on the destination, which copies the
    source directory's mode over the one `ensure_workspace` just set -- so the
    workspace inherits 0755 from the git checkout and the builder loses write
    access. Re-applying the permissions afterwards is not redundant.
    """
    ensure_workspace(project_id, clean=clean)
    shutil.copytree(source, workspace_path(project_id), dirs_exist_ok=True)
    return relax_permissions(project_id)


def list_files(project_id: str, *, include_build: bool = False) -> list[str]:
    base = workspace_path(project_id)
    if not base.is_dir():
        return []
    paths: list[str] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(base)
        if not include_build and relative.parts and relative.parts[0] == BUILD_DIR:
            continue
        paths.append(str(relative))
    return paths


def read_file(project_id: str, relative: str) -> str:
    target = safe_join(project_id, relative)
    if not target.is_file():
        raise WorkspaceError(f"no such file: {relative!r}")
    return target.read_text(encoding="utf-8", errors="replace")


def read_bytes(project_id: str, relative: str) -> bytes:
    target = safe_join(project_id, relative)
    if not target.is_file():
        raise WorkspaceError(f"no such file: {relative!r}")
    return target.read_bytes()


def remove_workspace(project_id: str) -> None:
    path = workspace_path(project_id)
    if path.exists():
        shutil.rmtree(path)
