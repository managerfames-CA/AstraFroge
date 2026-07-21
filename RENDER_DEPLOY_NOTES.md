# AstraForge Backend Render Deploy Notes

## Recommended Render settings

- Runtime: Python
- Build Command: `pip install .`
- Start Command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`

## Environment variables

Use `.env.render.example` as the Render key list. Do not paste placeholder values unchanged.

Important safety locks:

- `ASTRAFORGE_EXECUTION_ENABLED=false` keeps demo execution off.
- To allow demo execution, set it to `true` only after Binance Demo keys and risk settings are correct.
- `ASTRAFORGE_RISK_PER_TRADE_PERCENT=1` means 1% risk per trade.
- `ASTRAFORGE_RISK_PER_TRADE_PERCENT=0` blocks trading.
- `ASTRAFORGE_RISK_MAX_MARGIN_EXPOSURE_USDT=0` blocks trading.
- `ASTRAFORGE_EXECUTION_TAKE_PROFIT_R_MULTIPLE=0` blocks trading.

## CORS

After Vercel deploy, update:

`ASTRAFORGE_CORS_ORIGINS=["https://YOUR-VERCEL-APP.vercel.app"]`
