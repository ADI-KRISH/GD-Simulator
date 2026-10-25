"""`GET /v1/deps` — what GD needs from the platform, as every module answers it.

The shell's System page asks each module this. GD's one platform upstream is
the skill graph (M04), and only while the bridge is on; standalone, it needs
nothing from the platform and says so. Platform_Core's registry does the
probing, so GD reports in the same shape and with the same timeouts as the rest.

Purpose:    answer the platform's "what do you depend on, and is it up?" probe.
Why needed: the platform's System page shows every module's upstreams in one
            standard shape; a module that does not answer would look broken.
Contains:
    router    - the `/v1/deps` route table, mounted by `services.main`.
    registry  - builds (and caches) the single-entry registry describing the
                skill graph (M04) dependency.
    deps      - the endpoint: empty answer when standalone, live health
                statuses when running inside the platform.
Used by:    main (mounts the router).
Depends on: common/config (platform_bridge flag, M04 URL) and Platform_Core's
            `ai_core.dependencies`.
"""

from ai_core.dependencies import Dependency, DependencyRegistry, DependencyStatus  # Platform_Core
from fastapi import APIRouter  # router

from services.common.config import settings  # platform_bridge flag and M04 URL

router = APIRouter(tags=["ops"])

# Cached registry; rebuilt if the M04 URL setting changes.
_registry: DependencyRegistry | None = None


def registry() -> DependencyRegistry:
    """Return the dependency registry describing GD's one upstream, the skill graph (M04)."""
    global _registry
    # Rebuild only on first use or when the configured M04 address changed.
    if _registry is None or _registry.dependencies[0].base_url != settings.m04_url:
        _registry = DependencyRegistry(
            dependencies=[
                Dependency(
                    module_id="M04",
                    name="Skill Graph & MNEMOS Memory",
                    reason="records each discussion's scores as evidence on the skill graph",
                    on_missing=(
                        "Your discussions are still scored here; they do not show on your "
                        "skill graph until it is back."
                    ),
                    base_url=settings.m04_url,
                )
            ]
        )
    return _registry


# Sync on purpose: the registry probes with blocking I/O, so FastAPI runs this
# in its thread pool rather than on the event loop the discussion runs on.
@router.get("/v1/deps")
def deps() -> list[DependencyStatus] | dict:
    """Report upstream dependency health in the platform's standard shape."""
    # Standalone GD needs nothing from the platform, so say that and stop.
    if not settings.platform_bridge:
        return {
            "module": "M14",
            "dependencies": [],
            "note": "Running on its own, not as part of the platform: it needs nothing from it.",
        }
    # Inside the platform: actually probe M04 and report its health.
    return registry().statuses()
