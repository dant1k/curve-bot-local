# Curve Pools Telegram Bot

Показывает пулы Curve по сетям с полями как на сайте: Base vAPY, Volume, TVL и ссылки на swap.

## Быстрый старт
```bash
git clone https://github.com/dant1k/curve-bot-local.git
cd curve-bot-local
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # заполнить токен и chat id
python bot.py
Команды
	•	/ping
	•	/chains
	•	/ [limit] [sort]  (sort: volume|tvl|apy)
	•	/top  all [limit]

Переменные .env
	•	TELEGRAM_TOKEN — токен бота
	•	CHAT_ID — id чата/супергруппы
	•	CHAINS — через запятую (ethereum,arbitrum,polygon,…)
	•	INSECURE_SSL=1 — если нужно игнорировать SSL на локалке
	•	HIDE_ZERO=1 — скрывать пулы с volume=0 и tvl=0
