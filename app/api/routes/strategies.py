from fastapi import APIRouter
from app.strategies import catalog

router = APIRouter()


@router.get("/strategies")
def strategies():
    return {"strategies": catalog(), "signals": ["BUY", "SELL", "HOLD", "EXIT"], "long_only": True}
