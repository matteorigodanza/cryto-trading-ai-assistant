"""
Strategy Guardian AI - FastAPI application.
"""
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.config import get_settings
from app.database import get_db, init_db
from app.db.repository import (
    get_settings as get_db_settings,
    get_risk_rules,
    upsert_settings,
    list_alerts,
    list_trades,
    store_alert,
    store_trade,
)
from app.guardian import on_trade_opened, on_market_scan, get_connector
from app.models import (
    AccountSnapshot,
    ExchangeName,
    GuardianAlert,
    RiskRule,
    Trade,
    TradeSide,
)
from modules.strategy_validator import StrategyRules
from modules.ai_analysis import generate_trade_feedback, TradeFeedback
from sqlalchemy.ext.asyncio import AsyncSession


# ----- Request/Response models -----
class TradeOpenRequest(BaseModel):
    exchange: str = "binance"
    symbol: str
    side: str  # "long" | "short"
    size: float
    entry_price: float
    leverage: int = 1


class MarketScanRequest(BaseModel):
    symbol: str
    current_price: float
    liquidation_cluster_price: Optional[float] = None
    funding_rate: Optional[float] = None
    volatility_24h: Optional[float] = None


class TradeFeedbackRequest(BaseModel):
    symbol: str
    side: str
    size: float
    entry_price: float
    exit_price: float
    leverage: int = 1
    pnl: float


class RiskRuleUpdate(BaseModel):
    max_risk_per_trade_pct: Optional[float] = None
    max_daily_loss_pct: Optional[float] = None
    max_leverage: Optional[int] = None
    max_position_pct_of_balance: Optional[float] = None


class StrategyRuleUpdate(BaseModel):
    strategy_name: Optional[str] = None
    entry_conditions: Optional[Dict[str, Any]] = None
    exit_conditions: Optional[Dict[str, Any]] = None


class SettingsUpdate(BaseModel):
    risk: Optional[RiskRuleUpdate] = None
    strategy: Optional[StrategyRuleUpdate] = None


class WebhookTradePayload(BaseModel):
    """Payload when exchange or TradingView sends a new trade event."""
    exchange: str = "binance"
    symbol: str
    side: str
    size: float
    entry_price: float
    leverage: int = 1


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title="Strategy Guardian AI",
    description="AI-powered guardian for crypto traders. Monitor risk, detect emotional trading, validate strategy rules.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "Strategy Guardian AI"}


async def _run_guardian_and_persist(
    trade: Trade,
    db: AsyncSession,
) -> List[GuardianAlert]:
    """Load settings from DB, run guardian, persist alerts and trade."""
    risk_rules = await get_risk_rules(db)
    row = await get_db_settings(db)
    strategy_rules = None
    if row:
        strategy_rules = StrategyRules(
            name=row.strategy_name,
            entry_conditions=row.entry_conditions or {},
            exit_conditions=row.exit_conditions or {},
        )
    connector = get_connector(trade.exchange.value)
    account = await connector.get_account_snapshot() if connector else None
    recent_trades = await connector.get_recent_trades(limit=20) if connector else []
    alerts = await on_trade_opened(
        trade=trade,
        account=account,
        risk_rules=risk_rules,
        strategy_rules=strategy_rules,
        recent_trades=recent_trades,
        send_to_telegram_discord=True,
    )
    for a in alerts:
        await store_alert(db, a)
    await store_trade(db, trade)
    return alerts


@app.post("/guardian/on-trade-opened", response_model=List[Dict[str, Any]])
async def api_on_trade_opened(req: TradeOpenRequest, db: AsyncSession = Depends(get_db)):
    """
    Simulate or forward a trade open event. Runs risk + strategy + emotion checks and sends alerts.
    Persists alerts and trade for the dashboard.
    """
    trade = Trade(
        exchange=ExchangeName.BINANCE if req.exchange.lower() == "binance" else ExchangeName.BYBIT,
        symbol=req.symbol,
        side=TradeSide.LONG if req.side.lower() == "long" else TradeSide.SHORT,
        size=req.size,
        entry_price=req.entry_price,
        leverage=req.leverage,
    )
    alerts = await _run_guardian_and_persist(trade, db)
    return [a.model_dump(mode="json") for a in alerts]


@app.post("/guardian/market-scan")
async def api_market_scan(req: MarketScanRequest):
    """Run market risk scan (liquidation cluster, funding, volatility)."""
    result, alerts = await on_market_scan(
        symbol=req.symbol,
        current_price=req.current_price,
        liquidation_cluster_price=req.liquidation_cluster_price,
        funding_rate=req.funding_rate,
        volatility_24h=req.volatility_24h,
        send_alerts=True,
    )
    return {
        "result": result.model_dump(),
        "alerts": [a.model_dump(mode="json") for a in alerts],
    }


@app.post("/guardian/trade-feedback", response_model=TradeFeedback)
async def api_trade_feedback(req: TradeFeedbackRequest):
    """Get AI feedback for a closed trade."""
    from datetime import datetime
    trade = Trade(
        exchange=ExchangeName.BINANCE,
        symbol=req.symbol,
        side=TradeSide.LONG if req.side.lower() == "long" else TradeSide.SHORT,
        size=req.size,
        entry_price=req.entry_price,
        exit_price=req.exit_price,
        pnl=req.pnl,
        leverage=req.leverage,
        closed_at=datetime.utcnow(),
    )
    feedback = generate_trade_feedback(trade, strategy_compliance_pct=80.0)
    return feedback


@app.get("/account/{exchange}")
async def api_get_account(exchange: str):
    """Get account snapshot from exchange (for dashboard)."""
    connector = get_connector(exchange)
    if not connector:
        raise HTTPException(status_code=400, detail=f"Unknown or unconfigured exchange: {exchange}")
    acc = await connector.get_account_snapshot()
    if not acc:
        raise HTTPException(status_code=502, detail="Could not fetch account")
    return acc.model_dump(mode="json")


@app.get("/positions/{exchange}")
async def api_get_positions(exchange: str):
    """Get open positions (for dashboard)."""
    connector = get_connector(exchange)
    if not connector:
        raise HTTPException(status_code=400, detail=f"Unknown or unconfigured exchange: {exchange}")
    positions = await connector.get_open_positions()
    return [p.model_dump(mode="json") for p in positions]


# ----- Persistent settings & dashboard data -----

@app.get("/api/settings")
async def api_get_settings(db: AsyncSession = Depends(get_db)):
    """Get current risk and strategy settings (for dashboard)."""
    row = await get_db_settings(db)
    if not row:
        return {
            "risk": RiskRule().model_dump(),
            "strategy": {"strategy_name": "default", "entry_conditions": {}, "exit_conditions": {}},
        }
    return {
        "risk": {
            "max_risk_per_trade_pct": row.max_risk_per_trade_pct,
            "max_daily_loss_pct": row.max_daily_loss_pct,
            "max_leverage": row.max_leverage,
            "max_position_pct_of_balance": row.max_position_pct_of_balance,
        },
        "strategy": {
            "strategy_name": row.strategy_name,
            "entry_conditions": row.entry_conditions or {},
            "exit_conditions": row.exit_conditions or {},
        },
    }


@app.put("/api/settings")
async def api_put_settings(
    body: SettingsUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update risk and/or strategy settings."""
    risk_rule = None
    if body.risk:
        current = await get_risk_rules(db)
        risk_rule = RiskRule(
            max_risk_per_trade_pct=body.risk.max_risk_per_trade_pct or current.max_risk_per_trade_pct,
            max_daily_loss_pct=body.risk.max_daily_loss_pct or current.max_daily_loss_pct,
            max_leverage=body.risk.max_leverage or current.max_leverage,
            max_position_pct_of_balance=body.risk.max_position_pct_of_balance or current.max_position_pct_of_balance,
        )
    await upsert_settings(
        db,
        risk_rule=risk_rule,
        strategy_name=body.strategy.strategy_name if (body.strategy and body.strategy.strategy_name is not None) else None,
        entry_conditions=body.strategy.entry_conditions if (body.strategy and body.strategy.entry_conditions is not None) else None,
        exit_conditions=body.strategy.exit_conditions if (body.strategy and body.strategy.exit_conditions is not None) else None,
    )
    return {"ok": True}


@app.get("/api/alerts")
async def api_list_alerts(limit: int = 100, db: AsyncSession = Depends(get_db)):
    """List recent alerts (for dashboard feed)."""
    rows = await list_alerts(db, limit=limit)
    return [
        {
            "id": r.id,
            "level": r.level,
            "title": r.title,
            "message": r.message,
            "source": r.source,
            "trade_snapshot": r.trade_snapshot,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@app.get("/api/trades")
async def api_list_trades(
    exchange: Optional[str] = None,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
):
    """List stored trades (for dashboard history)."""
    rows = await list_trades(db, exchange=exchange, limit=limit)
    return [
        {
            "id": r.id,
            "exchange": r.exchange,
            "symbol": r.symbol,
            "side": r.side,
            "size": r.size,
            "entry_price": r.entry_price,
            "leverage": r.leverage,
            "opened_at": r.opened_at.isoformat(),
            "closed_at": r.closed_at.isoformat() if r.closed_at else None,
            "exit_price": r.exit_price,
            "pnl": r.pnl,
        }
        for r in rows
    ]


# ----- Webhook: exchange / TradingView can POST new trade here -----

@app.post("/webhook/trade")
async def webhook_trade(payload: WebhookTradePayload, db: AsyncSession = Depends(get_db)):
    """
    Receive new trade from exchange webhook or TradingView alert.
    Runs full guardian pipeline and persists alerts + trade.
    """
    trade = Trade(
        exchange=ExchangeName.BINANCE if payload.exchange.lower() == "binance" else ExchangeName.BYBIT,
        symbol=payload.symbol,
        side=TradeSide.LONG if payload.side.lower() == "long" else TradeSide.SHORT,
        size=payload.size,
        entry_price=payload.entry_price,
        leverage=payload.leverage,
    )
    alerts = await _run_guardian_and_persist(trade, db)
    return {
        "received": True,
        "alerts_count": len(alerts),
        "alerts": [a.model_dump(mode="json") for a in alerts],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
