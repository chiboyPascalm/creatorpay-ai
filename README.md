# CreatorPay AI

CreatorPay AI is an independent creator-commerce prototype for developing digital products with AI, preparing product listings, and accepting PayPal payments.

## Features in this prototype

- AI-assisted digital-product drafting through the Gemini API.
- Creator-managed product records stored in SQLite.
- PayPal Orders v2 order creation and server-side capture.
- Server-side price lookup and capture verification against the saved order amount and currency.
- PayPal Sandbox mode by default.

## Local setup

1. Copy `.env.example` to `.env` if you are starting from a fresh environment.
2. Add your Gemini API key to `GEMINI_API_KEY`.
3. Keep PayPal credentials in `.env`; never put the PayPal secret in frontend code.
4. Install dependencies: `pip install -r requirements.txt`
5. Run: `python app.py`
6. Open `http://127.0.0.1:5000`.

## Important

This is a hackathon development prototype, not a production marketplace. SQLite is suitable for local testing but should be replaced with managed persistent storage for deployment. Creator authentication, buyer entitlements/download delivery, rate limiting, webhook reconciliation, and production security hardening are still required before public launch. Sandbox payments are test transactions, not real payments.
