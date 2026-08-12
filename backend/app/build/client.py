"""HTTP client for the build sandbox (M4).

Shaped like `PageVaultClient` on purpose: one module owns the wire format, a
transport can be injected so tests never need a cross toolchain, and failure
degrades into a value instead of an exception.

That last rule matters more here than anywhere else. A compile that fails is
not an error in the pipeline -- it is the *answer*, and it still has to reach
the user together with the project that produced it.
"""

import logging
from functools import lru_cache
from typing import Any

import httpx

from app.build.diagnostics import parse_log, parse_size
from app.core.config import settings
from app.orchestrator.contracts import (
    BUILD_TIMEOUT,
    BUILD_UNAVAILABLE,
    BuildResult,
)

logger = logging.getLogger(__name__)

# The HTTP call has to outlive the compile it is waiting for. Without the
# margin the client gives up while the sandbox is still working, and the run
# records a timeout that never happened.
CLIENT_TIMEOUT_MARGIN = 30.0
# Keep enough of the log for a human to read; the structured diagnostics are
# what code consumes.
LOG_TAIL_CHARS = 4000


def _tail(text: str, limit: int = LOG_TAIL_CHARS) -> str:
    if len(text) <= limit:
        return text
    return "[... truncated ...]\n" + text[-limit:]


class BuilderClient:
    """Async client for the isolated build service."""

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or settings.builder_url).rstrip("/")
        self.timeout = timeout or settings.build_timeout_seconds + CLIENT_TIMEOUT_MARGIN
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def health(self) -> dict[str, Any] | None:
        """Sandbox status, or None when it cannot be reached."""
        try:
            response = await self._client.get("/health")
        except Exception as exc:  # noqa: BLE001 - unreachable is a state, not a bug
            logger.warning("build sandbox unreachable at %s: %s", self.base_url, exc)
            return None
        if response.status_code != 200:
            return None
        return response.json()

    async def build(
        self,
        project_id: str,
        *,
        target: str = "",
        clean: bool = False,
        attempt: int = 1,
        timeout_seconds: float | None = None,
        flash_total: int = 0,
        ram_total: int = 0,
    ) -> BuildResult:
        """Compile one workspace. Never raises."""
        payload = {
            "project_id": str(project_id),
            "target": target,
            "clean": clean,
            "timeout_seconds": timeout_seconds or settings.build_timeout_seconds,
        }
        try:
            response = await self._client.post("/build", json=payload)
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException as exc:
            return _degraded(BUILD_TIMEOUT, attempt, f"build request timed out: {exc}")
        except Exception as exc:  # noqa: BLE001 - see module docstring
            return _degraded(BUILD_UNAVAILABLE, attempt, f"build sandbox unavailable: {exc}")

        return result_from_payload(
            data,
            project_id=str(project_id),
            attempt=attempt,
            flash_total=flash_total,
            ram_total=ram_total,
        )


def _degraded(status: str, attempt: int, message: str) -> BuildResult:
    logger.warning("%s", message)
    return BuildResult(status=status, exit_code=-1, attempt=attempt, log_tail=message)


def result_from_payload(
    data: dict[str, Any],
    *,
    project_id: str,
    attempt: int = 1,
    flash_total: int = 0,
    ram_total: int = 0,
) -> BuildResult:
    """Wire format -> contract. The sandbox reports; the backend interprets."""
    log = str(data.get("log") or "")
    workspace_root = f"{settings.workspace_root.rstrip('/')}/{project_id}"
    return BuildResult(
        status=str(data.get("status") or BUILD_UNAVAILABLE),
        exit_code=int(data.get("exit_code") or 0),
        duration_ms=int(data.get("duration_ms") or 0),
        toolchain=str(data.get("toolchain") or ""),
        command=str(data.get("command") or ""),
        attempt=attempt,
        artifacts={str(k): str(v) for k, v in (data.get("artifacts") or {}).items()},
        size=parse_size(
            str(data.get("size_output") or ""),
            flash_total=flash_total,
            ram_total=ram_total,
        ),
        diagnostics=parse_log(log, root=workspace_root),
        log_tail=_tail(log),
    )


@lru_cache(maxsize=1)
def get_builder_client() -> BuilderClient:
    return BuilderClient()


async def close_builder_client() -> None:
    """Close the shared client (app shutdown, end of a Celery task)."""
    if get_builder_client.cache_info().currsize:
        await get_builder_client().aclose()
        get_builder_client.cache_clear()
