# Stock Predictor Monorepo

Starter scaffold for an NSE/BSE stock prediction stack with:
- Next.js frontend
- FastAPI Python backend
- Node.js WebSocket server
- MongoDB via Docker Compose
- Research notebooks

This is a baseline for daily data first. Intraday can be added later.

## Quickstart

1. Install Node.js and Python 3.11+.
2. Optional: start MongoDB with `docker compose -f infra/docker-compose.yml up -d`.
3. Run `npm install`.
4. Run `python -m venv .venv`.
5. Run `.venv\Scripts\activate`.
6. Run `pip install -r services/api/requirements.txt`.
7. Run `npm run dev:web` in one terminal.
8. Run `npm run dev:api` in one terminal.
9. Run `npm run dev:ws` in one terminal.

## Notebooks

- `notebooks/01_daily_bhavcopy.ipynb` shows how to pull daily bhavcopy for NSE+BSE.

## URLs

- Web: http://localhost:3000
- API: http://localhost:8000/health
- WS: ws://localhost:4001
