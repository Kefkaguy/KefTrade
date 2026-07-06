from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import backtests, data, features, research, risk, signals, symbols

app = FastAPI(title="KefTrade API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:3000", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(symbols.router)
app.include_router(data.router)
app.include_router(features.router)
app.include_router(signals.router)
app.include_router(backtests.router)
app.include_router(research.router)
app.include_router(risk.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
