## Setup and Run

```bash
mkdir telegram_crickbot
cd telegram_crickbot


git init
git remote add origin https://github.com/<your-username>/<your-repo>.git

git pull origin main

docker build -t telegram_crickbot .

docker run -d --name crickbot \
  --env-file .env \
  -v ~/crickbot_data:/app/data \
  telegram_crickbot

##Logs
docker logs -f crickbot

```

Make Sure to add .env file in the same directory of project folder

```bash
nano .env
```

✅ This gives a clear step‑by‑step workflow: initialize Git, pull the repo, build the Docker image, run with environment variables and volume mount, and check logs.  

Would you like me to also add a **docker-compose section** in the README so users can run everything with a single `docker-compose up -d` instead of manual build/run commands?







# Tournament Telegram Bot

This bot lets you add match results and view a tournament table with:
- points table
- purple cap (most wickets)
- orange cap (most runs)
- run rate and strike rate
- win/draw/loss points
- net run rate as a tie-breaker

## Usage

### Console mode (works immediately)
Run:

```bash
python bot.py
```

Then enter results like:

```text
India 180/6 120 vs Pakistan 175/7 110
```

### Telegram mode
1. Create a bot with BotFather.
2. Put the token in .env:

```env
TELEGRAM_BOT_TOKEN=your_token_here
```
3. Install the dependency:

```bash
pip install python-telegram-bot python-dotenv
```
4. Run:

```bash
python bot.py
```

Commands:
- /start
- /help
- /addmatch TeamA 180/6 120 vs TeamB 175/7 110
- /standings
- /caps
