"""`GET /v1/deps` — what GD needs from the platform, as every module answers it.

The shell's System page asks each module this. GD's one platform upstream is
the skill graph (M04), and only while the bridge is on; standalone, it needs
nothing from the platform and says so. Platform_Core's registry does the
probing, so GD reports in the same shape and with the same timeouts as the rest.
"""

from ai_core.dependencies import Dependency, DependencyRegistry, DependencyStatus
from fastapi import APIRouter

from services.common.config import settings

router = APIRouter(tags=["ops"])

_registry: DependencyRegistry | None = None


def registry() -> DependencyRegistry:
    global _registry
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
    if not settings.platform_bridge:
        return {
            "module": "M14",
            "dependencies": [],
            "note": "Running on its own, not as part of the platform: it needs nothing from it.",
        }
    return registry().statuses()
