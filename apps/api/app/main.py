from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import alpha, backtests, data, features, paper, regimes, research, research_copilot, research_intelligence, research_lab, risk, signals, symbols, validation
from app.settings import cors_origin_list
from app.services.paper_scheduler import start_scheduler, stop_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    try:
        yield
    finally:
        await stop_scheduler()


app = FastAPI(title="KefTrade API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origin_list(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(symbols.router)
app.include_router(data.router)
app.include_router(features.router)
app.include_router(regimes.router)
app.include_router(signals.router)
app.include_router(backtests.router)
app.include_router(research.router)
app.include_router(research_lab.router)
app.include_router(research_intelligence.router)
app.include_router(research_copilot.router)
app.include_router(alpha.router)
app.include_router(validation.router)
app.include_router(risk.router)
app.include_router(paper.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
