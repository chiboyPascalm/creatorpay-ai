# CreatorPay AI

CreatorPay AI is an independent creator-commerce prototype for developing digital products with AI, preparing product listings, and accepting PayPal payments.

## Features in this prototype

- AI-assisted digital-product drafting with Groq as the primary provider and Google Gemini as automatic fallback.
- Creator-managed product records stored in SQLite.
- PayPal Orders v2 order creation and server-side capture.
- Server-side price lookup and capture verification against the saved order amount and currency.
- PayPal Sandbox mode by default.

## Local setup

1. Copy `.env.example` to `.env` if you are starting from a fresh environment.
2. Add your Groq API key to `GROQ_API_KEY` (primary provider).
3. Optionally add your Google Gemini key to `GEMINI_API_KEY` for automatic fallback.
4. The default models are `openai/gpt-oss-20b` on Groq and `gemini-3.5-flash-lite` on Gemini.
5. Keep PayPal credentials in `.env`; never put the PayPal secret in frontend code.
6. Install dependencies: `pip install -r requirements.txt`
7. Run: `python app.py`
8. Open `http://127.0.0.1:5000`.

## Important

This is a hackathon development prototype, not a production marketplace. SQLite is suitable for local testing but should be replaced with managed persistent storage for deployment. Creator authentication, buyer entitlements/download delivery, rate limiting, webhook reconciliation, and production security hardening are still required before public launch. Sandbox payments are test transactions, not real payments.
