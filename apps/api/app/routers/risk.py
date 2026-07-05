from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
import psycopg

from app.db import get_connection

router = APIRouter(tags=["risk"])


class RiskSettingsUpdate(BaseModel):
    account_size: Decimal = Field(gt=0)
    max_risk_per_trade: Decimal = Field(gt=0, le=Decimal("0.01"))
    max_open_exposure: Decimal = Field(gt=0, le=Decimal("0.03"))
    daily_loss_limit: Decimal = Field(gt=0, le=Decimal("0.02"))
    weekly_loss_limit: Decimal = Field(gt=0, le=Decimal("0.05"))


@router.get("/risk/settings")
def get_risk_settings(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM risk_settings WHERE id = 1").fetchone()
    return dict(row)


@router.put("/risk/settings")
def update_risk_settings(
    payload: RiskSettingsUpdate,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    row = conn.execute(
        """
        UPDATE risk_settings
        SET account_size = %s,
            max_risk_per_trade = %s,
            max_open_exposure = %s,
            daily_loss_limit = %s,
            weekly_loss_limit = %s,
            allow_leverage = FALSE,
            allow_live_trading = FALSE,
            updated_at = NOW()
        WHERE id = 1
        RETURNING *
        """,
        (
            payload.account_size,
            payload.max_risk_per_trade,
            payload.max_open_exposure,
            payload.daily_loss_limit,
            payload.weekly_loss_limit,
        ),
    ).fetchone()
    conn.commit()
    return dict(row)

