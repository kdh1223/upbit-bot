# upbit_bot 운영 가이드

## 실행 (screen 방식)
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

## screen 내부 로깅 토글
- `Ctrl+A` -> `H`

## requests 설치
```bash
pip install requests
```

## 텔레그램 설정 (.env)
```bash
export TELEGRAM_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
```

## systemd 알림 적용 (재시작/자동복구/크래시 분리)

### 1) 환경 파일 생성
```bash
sudo install -m 0644 deploy/systemd/telegram-bot.env.example /etc/default/telegram-bot
sudoedit /etc/default/telegram-bot
```

`/etc/default/telegram-bot` 예시:
```bash
TELEGRAM_TOKEN=123456:ABCDEF
TELEGRAM_CHAT_ID=123456789
```

### 2) 실패 알림 oneshot 유닛 설치
```bash
sudo install -m 0644 deploy/systemd/telegram-fail-notify@.service /etc/systemd/system/telegram-fail-notify@.service
```

### 3) upbit-bot override 적용
```bash
sudo systemctl edit upbit-bot
```

위 편집기에 `deploy/systemd/upbit-bot.override.conf` 내용을 그대로 붙여넣습니다.

### 4) 반영
```bash
sudo systemctl daemon-reload
sudo systemctl restart upbit-bot
sudo systemctl status upbit-bot --no-pager
sudo journalctl -u upbit-bot -n 200 --no-pager
```

### 5) 안전 크래시 테스트
```bash
PID=$(systemctl show -p MainPID --value upbit-bot)
kill -SEGV "$PID"
sudo journalctl -u upbit-bot -n 200 --no-pager
```

기대 알림:
- 정상 시작: `🟢 봇 시작됨`
- 수동 재시작: `🔁 봇 재시작됨`
- 크래시 발생: `🔴 봇 비정상 종료(크래시)`
- 크래시 후 자동 재기동: `⚠️ 비정상 종료 후 자동 복구됨`
