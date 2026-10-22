"""Safe, current-project operator-policy projection (no entitlement prerequisite)."""
from fastapi import APIRouter, Depends

from lumen.auth import get_principal
from lumen.policy import authorize
from lumen.service_authority import SCOPE_CAPABILITIES, allowed_scopes

router = APIRouter()


@router.get("/policy/capabilities")
async def policy_capabilities(principal: dict = Depends(get_principal)) -> dict:
    actions = set(allowed_scopes(principal))
    if authorize("system:admin", principal):
        actions.add("system:admin")
    labels = {leaves[0] for action, leaves in SCOPE_CAPABILITIES.items() if len(leaves) == 1 and action in actions}
    return {"service": "lumen", "capabilities": sorted(labels), "actions": sorted(actions)}
