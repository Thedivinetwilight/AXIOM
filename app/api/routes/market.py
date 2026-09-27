from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/market/status")
async def status(request: Request):
    return request.app.state.simulator.status()


@router.get("/market/datasets")
def datasets(request: Request):
    root = request.app.state.data_dir
    return {
        "datasets": [p.name for p in sorted(root.glob("*.csv")) if not p.is_symlink()],
        "note": "CSV timeframes must be declared correctly; no implicit resampling",
    }
