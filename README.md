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
