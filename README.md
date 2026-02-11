# upbit_bot 운영 가이드

## 실행 (screen)
```bash
cd ~/upbit-bot && chmod +x run_bot.sh && ./run_bot.sh
```

## screen 분리
- `Ctrl+A` -> `D`

## 재접속
```bash
screen -r bot
```

## 세션 목록
```bash
screen -list
```

## 종료
```bash
screen -S bot -X quit
```

## 로그 확인
```bash
tail -n 200 bot_console.log
tail -f bot_console.log
```

## 의존성 설치
```bash
pip install pyupbit python-dotenv requests
```

## 환경 변수 (.env)
```bash
UPBIT_ACCESS=your_access_key
UPBIT_SECRET=your_secret_key
TELEGRAM_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id
```

## systemd override 적용
1. override 열기
```bash
sudo systemctl edit upbit-bot
```
2. `deploy/systemd/upbit-bot.override.conf` 내용 붙여넣기
3. 반영
```bash
sudo systemctl daemon-reload
sudo systemctl restart upbit-bot
sudo systemctl status upbit-bot --no-pager
sudo journalctl -u upbit-bot -n 200 --no-pager
```

## Daily report and heartbeat schedule (KST-safe)
`run_daily_report.py` now enforces KST send windows and day-level dedupe by default.

- Daily report target: `21:00 KST`
- Heartbeat target: `09:00 KST`
- Allowed window: `+/- 30 minutes` (configurable by `REPORT_SCHEDULE_WINDOW_MIN` or `--schedule-window-min`)
- Manual override: add `--force`

Recommended cron on UTC servers:
```bash
# 21:00 KST == 12:00 UTC
0 12 * * * cd ~/upbit-bot && ./.venv/bin/python run_daily_report.py

# 09:00 KST == 00:00 UTC
0 0 * * * cd ~/upbit-bot && ./.venv/bin/python run_daily_report.py --heartbeat-only
```

Alternative (safer against missed cron runs):
```bash
*/5 * * * * cd ~/upbit-bot && ./.venv/bin/python run_daily_report.py --scheduled-report
*/5 * * * * cd ~/upbit-bot && ./.venv/bin/python run_daily_report.py --scheduled-heartbeat
```

If you need to send immediately (outside schedule window), run:
```bash
cd ~/upbit-bot && ./.venv/bin/python run_daily_report.py --force
cd ~/upbit-bot && ./.venv/bin/python run_daily_report.py --heartbeat-only --force
```

## Notification & Risk Diagnostics
Quick checks after deployment:

1. Telegram order event smoke test
```bash
./.venv/bin/python - <<'PY'
from utils.telegram_notify import notify_order
notify_order(
    event_type="ORDER_BUY_FILLED",
    strategy_tag="MAIN",
    ticker="KRW-TEST",
    price=1234,
    qty=0.1234,
    reason="ENTRY",
)
PY
```

2. Risk-cut single-alert behavior (no repeated alerts on restart)
```bash
./.venv/bin/python - <<'PY'
import datetime as dt
import bot
state = bot._normalize_runtime_risk_state({})
now = dt.datetime.now()
bot._update_global_risk_cut_state(now=now, equity=100000, risk_state=state, holdings_count=0)
info, _, _ = bot._update_global_risk_cut_state(now=now, equity=60000, risk_state=state, holdings_count=0)
bot._notify_risk_cut_once(info, 60000, state)
PY
```

3. Market warning filter check (caution should be blocked)
```bash
./.venv/bin/python - <<'PY'
import market
active, inactive, reasons = market.filter_tradeable_tickers(
    ["KRW-ZRO"], {"KRW-ZRO": {"market_warning": "CAUTION"}}, strict_registry=True
)
print(active, inactive, reasons)
PY
```
