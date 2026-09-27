from fastapi import HTTPException
from app.data.historical import CSVProvider


def load_dataset(app, body):
    root = app.state.data_dir.resolve()
    path = (root / body.dataset).resolve()
    if path.parent != root or not path.is_file():
        raise HTTPException(404, "Dataset not found")
    if path.stat().st_size > 5_000_000:
        raise HTTPException(413, "Dataset too large for synchronous API; use CLI")
    bars = CSVProvider(path, body.timeframe).historical(body.symbol, body.timeframe)
    if len(bars) > 10_000:
        raise HTTPException(413, "API limited to 10000 candles; use CLI")
    return bars
