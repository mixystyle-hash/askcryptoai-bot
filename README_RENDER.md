# AskCryptoAI — Render 24/7 Deployment

This repo is ready to deploy your Telegram bot on **Render** as a 24/7 background worker.

## Files
- `render.yaml` — Render config (worker service, Python, start command)
- `main.py`, `requirements.txt`, `.env.example` — your bot code (copy from optimized project)

## Steps
1) Create a new **GitHub repository** and upload: `main.py`, `requirements.txt`, `.env.example`, and `render.yaml`.
2) On **Render**: New → **Background Worker** → connect the GitHub repo.
3) Render will detect `render.yaml`. Confirm the service.
4) In **Environment Variables** set:
   - `TELEGRAM_BOT_TOKEN` (from @BotFather)
   - `OPENAI_API_KEY` (your OpenAI key)
   - (optional) other limits/pricing already pre-filled in render.yaml
5) Click **Create Worker**. Build installs deps and runs `python main.py`.
6) Send `/start` to your bot — it now runs **24/7**.

Notes:
- Using **worker** type (not web) because Telegram bots use long polling and don't expose a port.
- Auto deploys on every push to GitHub (`autoDeploy: true`).
