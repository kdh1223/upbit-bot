# upbit_bot 운영 가이드

## 실행(권장)
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

## requests 설치 안내
```bash
pip install requests
```

## 텔레그램 설정 방법(.env 예시)
```bash
export TELEGRAM_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
```
