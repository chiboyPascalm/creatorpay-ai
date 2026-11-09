from pathlib import Path
from datetime import datetime
import shutil
import json
import re

app_path = Path("app.py")
env_path = Path(".env.example")
readme_path = Path("README.md")

app = app_path.read_text()
env = env_path.read_text()
readme = readme_path.read_text()

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = Path("backups") / f"provider-switch-{stamp}"
backup.mkdir(parents=True, exist_ok=True)

for p in (app_path, env_path, readme_path):
    shutil.copy2(p, backup / p.name)

def once(s, old, new, label):
    n = s.count(old)
    if n != 1:
        raise RuntimeError(f"{label}: expected 1 match, found {n}")
    return s.replace(old, new, 1)

# Add Groq model configuration.
app = once(
    app,
    'GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")',
    'GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")\n'
    'GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")',
    "Groq model setting"
)

# Replace the old Gemini helper with a shared provider adapter.
start = app.index("def gemini_post(")
end = app.index("\ndef delivery_token(", start)

helper = '''
def gemini_post(prompt, generation_config, timeout, operation):
    """Call Gemini with bounded retries for temporary provider failures."""
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
    )
    headers = {"x-goog-api-key": os.getenv("GEMINI_API_KEY", "")}
    retryable_statuses = {500, 502, 503, 504}

    for attempt in range(4):
        try:
            response = requests.post(
                url,
                headers=headers,
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": generation_config,
                },
                timeout=timeout,
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            app.logger.warning(
                "Gemini %s attempt %s/4 network failure: %s",
                operation, attempt + 1, type(exc).__name__
            )
            if attempt == 3:
                raise
            time.sleep(min(2 ** (attempt + 1), 12))
            continue

        if response.status_code not in retryable_statuses:
            return response

        app.logger.warning(
            "Gemini %s attempt %s/4 returned HTTP %s",
            operation, attempt + 1, response.status_code
        )
        if attempt == 3:
            return response

        retry_after = response.headers.get("Retry-After", "")
        try:
            delay = min(max(float(retry_after), 1), 20)
        except (TypeError, ValueError):
            delay = min(2 ** (attempt + 1), 12)
        time.sleep(delay)

    return response


def ai_post(prompt, generation_config, timeout, operation):
    """Try Groq first, then automatically fall back to Gemini."""
    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    groq_failure = "Groq is not configured."

    if groq_key:
        try:
            response = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {groq_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": GROQ_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": generation_config.get("temperature", 0.65),
                    "max_tokens": generation_config.get(
                        "maxOutputTokens", 2500
                    ),
                },
                timeout=timeout,
            )

            if response.ok:
                body = response.json()
                content = (
                    body.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
                if isinstance(content, str) and content.strip():
                    normalized = requests.Response()
                    normalized.status_code = 200
                    normalized.url = response.url
                    normalized.headers["Content-Type"] = "application/json"
                    normalized.headers["X-AI-Provider"] = "Groq"
                    normalized.headers["X-AI-Model"] = GROQ_MODEL
                    normalized._content = json.dumps({
                        "candidates": [{
                            "content": {
                                "parts": [{"text": content.strip()}]
                            }
                        }]
                    }).encode("utf-8")
                    app.logger.info(
                        "%s completed with Groq model %s",
                        operation, GROQ_MODEL
                    )
                    return normalized

                groq_failure = "Groq returned an empty response."
            else:
                groq_failure = f"Groq returned HTTP {response.status_code}."

        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
            groq_failure = f"Groq request failed ({type(exc).__name__})."

        app.logger.warning(
            "%s: %s Trying Gemini fallback.",
            operation, groq_failure
        )

    if gemini_key:
        response = gemini_post(
            prompt, generation_config, timeout, operation
        )
        response.headers["X-AI-Provider"] = "Google Gemini"
        response.headers["X-AI-Model"] = GEMINI_MODEL
        return response

    unavailable = requests.Response()
    unavailable.status_code = 503
    unavailable._content = json.dumps({
        "error": {"message": groq_failure + " Gemini fallback is not configured."}
    }).encode("utf-8")
    unavailable.headers["X-AI-Provider"] = "Unavailable"
    unavailable.headers["X-AI-Model"] = "none"
    return unavailable


def ai_model(response):
    return response.headers.get("X-AI-Model", GEMINI_MODEL)


'''

app = app[:start] + helper + app[end:]

# Health reports provider configuration without exposing credentials.
app = once(
    app,
    '"ai_configured": bool(os.getenv("GEMINI_API_KEY")),',
    '"ai_configured": bool(os.getenv("GROQ_API_KEY") or os.getenv("GEMINI_API_KEY")),\n'
    '        "ai_primary_provider": "Groq",\n'
    '        "ai_providers": {\n'
    '            "groq": bool(os.getenv("GROQ_API_KEY")),\n'
    '            "gemini": bool(os.getenv("GEMINI_API_KEY")),\n'
    '        },',
    "health endpoint"
)

# Both routes must accept either provider.
app = once(
    app,
    '    api_key = os.getenv("GEMINI_API_KEY", "")\n'
    '    if not api_key:\n'
    '        return error("AI is not connected yet. Add your Gemini API key to .env.", 503)',
    '    if not (os.getenv("GROQ_API_KEY") or os.getenv("GEMINI_API_KEY")):\n'
    '        return error("AI is not connected. Configure Groq or Gemini.", 503)',
    "outline API-key check"
)

app = once(
    app,
    '    api_key = os.getenv("GEMINI_API_KEY", "")\n'
    '    if not api_key:\n'
    '        return error("Gemini API key is not configured.", 503)',
    '    if not (os.getenv("GROQ_API_KEY") or os.getenv("GEMINI_API_KEY")):\n'
    '        return error("AI is not connected. Configure Groq or Gemini.", 503)',
    "chapter API-key check"
)

# The adapter contains one intentional Gemini fallback call.
# Only replace the two generation route calls after the adapter.
marker = "def delivery_token("
if marker not in app:
    raise RuntimeError("Could not locate route section.")

helper_part, route_part = app.split(marker, 1)

if route_part.count("response = gemini_post(") != 2:
    raise RuntimeError(
        "Expected exactly two generation route calls after the adapter."
    )

route_part = route_part.replace(
    "response = gemini_post(",
    "response = ai_post(",
    2
)
app = helper_part + marker + route_part

app = once(
    app,
    '"model": GEMINI_MODEL\n        })',
    '"model": ai_model(response),\n            "provider": response.headers.get("X-AI-Provider", "Google Gemini")\n        })',
    "outline response metadata"
)

app = once(
    app,
    'return jsonify({"chapter": chapter, "model": GEMINI_MODEL})',
    'return jsonify({\n'
    '            "chapter": chapter,\n'
    '            "model": ai_model(response),\n'
    '            "provider": response.headers.get("X-AI-Provider", "Google Gemini")\n'
    '        })',
    "chapter response metadata"
)

# Update example environment variables without adding real secrets.
env = once(
    env,
    "GEMINI_API_KEY=\nGEMINI_MODEL=gemini-3.5-flash-lite",
    "GROQ_API_KEY=\nGROQ_MODEL=openai/gpt-oss-20b\n"
    "GEMINI_API_KEY=\nGEMINI_MODEL=gemini-3.5-flash-lite",
    "environment example"
)

readme = readme.replace(
    "- AI-assisted digital-product drafting through the Gemini API.",
    "- AI-assisted digital-product drafting with Groq as the primary provider "
    "and Google Gemini as automatic fallback."
)
readme = readme.replace(
    "2. Add your Gemini API key to GEMINI_API_KEY.",
    "2. Add your Groq API key to GROQ_API_KEY (primary provider).\n"
    "3. Optionally add your Google Gemini key to GEMINI_API_KEY for automatic fallback.\n"
    "4. The default Groq model is openai/gpt-oss-20b; Gemini uses gemini-3.5-flash-lite."
)
readme = readme.replace(
    "3. Keep PayPal credentials in .env; never put the PayPal secret in frontend code.\n"
    "4. Install dependencies: `pip install -r requirements.txt`\n"
    "5. Run: `python app.py`\n"
    "6. Open `http://127.0.0.1:5000`.",
    "5. Keep PayPal credentials in .env; never put the PayPal secret in frontend code.\n"
    "6. Install dependencies: `pip install -r requirements.txt`\n"
    "7. Run: `python app.py`\n"
    "8. Open `http://127.0.0.1:5000`."
)

# Write only after all expected source matches have passed.
app_path.write_text(app)
env_path.write_text(env)
readme_path.write_text(readme)

print("PATCH APPLIED")
print("Backup:", backup)
