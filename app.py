from __future__ import annotations

import asyncio
import hashlib
import html
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import edge_tts
import feedparser
import httpx
import yfinance as yf
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from groq import Groq
from openai import OpenAI
from pydantic import BaseModel

APP_NAME = "Morning Intelligence Briefing"
DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "").strip()
TIMEZONE_NAME = os.getenv("TIMEZONE", "America/Los_Angeles")
REPORT_MINUTES = int(os.getenv("REPORT_MINUTES", "15"))

# Provider selection: "openai" or "groq" for LLM, "openai" or "edge" for TTS
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "groq").lower()
TTS_PROVIDER = os.getenv("TTS_PROVIDER", "edge").lower()

VOICE = os.getenv("VOICE", "coral")  # OpenAI voice
EDGE_VOICE = os.getenv("EDGE_VOICE", "en-US-JennyNeural")  # Edge TTS voice
EDGE_RATE = os.getenv("EDGE_RATE", "+18%")  # Speed: +10% to +25% keeps engaged
TEXT_MODEL = os.getenv("TEXT_MODEL", "gpt-5-mini")  # OpenAI model
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-70b-versatile")  # Groq model
TTS_MODEL = os.getenv("TTS_MODEL", "gpt-4o-mini-tts")

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
app = FastAPI(title=APP_NAME)

# Major international/public-service outlets plus targeted Reuters/AP searches.
RSS_FEEDS = {
    "BBC World": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "BBC Business": "https://feeds.bbci.co.uk/news/business/rss.xml",
    "NPR World": "https://feeds.npr.org/1004/rss.xml",
    "NPR Business": "https://feeds.npr.org/1006/rss.xml",
    "DW World": "https://rss.dw.com/rdf/rss-en-world",
    "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",
    "Reuters World via Google News": (
        "https://news.google.com/rss/search?q="
        + quote_plus("site:reuters.com/world when:1d")
        + "&hl=en-US&gl=US&ceid=US:en"
    ),
    "Reuters Markets via Google News": (
        "https://news.google.com/rss/search?q="
        + quote_plus("site:reuters.com/markets OR site:reuters.com/business when:1d")
        + "&hl=en-US&gl=US&ceid=US:en"
    ),
    "AP World via Google News": (
        "https://news.google.com/rss/search?q="
        + quote_plus("site:apnews.com world when:1d")
        + "&hl=en-US&gl=US&ceid=US:en"
    ),
    "AP Business via Google News": (
        "https://news.google.com/rss/search?q="
        + quote_plus("site:apnews.com business markets when:1d")
        + "&hl=en-US&gl=US&ceid=US:en"
    ),
    "CNBC Markets": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
}

DEFAULT_TICKERS = ["^GSPC", "^DJI", "^IXIC", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AMD"]

class GenerateResponse(BaseModel):
    date: str
    audio_url: str
    transcript_url: str
    cached: bool


def require_token(token_query: str | None, authorization: str | None) -> None:
    if not ACCESS_TOKEN:
        raise HTTPException(500, "Server ACCESS_TOKEN is not configured.")
    bearer = ""
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()
    supplied = token_query or bearer
    if not supplied or supplied != ACCESS_TOKEN:
        raise HTTPException(401, "Invalid access token.")


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = html.unescape(re.sub(r"<[^>]+>", " ", value))
    return re.sub(r"\s+", " ", value).strip()


def fetch_feeds(max_per_feed: int = 5, max_total: int = 50) -> list[dict[str, str]]:
    stories: list[dict[str, str]] = []
    seen: set[str] = set()

    headers = {"User-Agent": "MorningIntelligenceBriefing/1.0"}
    with httpx.Client(timeout=15, follow_redirects=True, headers=headers) as http:
        for source, url in RSS_FEEDS.items():
            if len(stories) >= max_total:
                break
            try:
                response = http.get(url)
                response.raise_for_status()
                parsed = feedparser.parse(response.content)
            except Exception as exc:
                print(f"Feed failed: {source}: {exc}")
                continue

            for item in parsed.entries[:max_per_feed]:
                if len(stories) >= max_total:
                    break
                title = clean_text(item.get("title"))
                link = item.get("link", "")
                summary = clean_text(item.get("summary") or item.get("description"))
                published = clean_text(item.get("published") or item.get("updated"))
                key = hashlib.sha256(title.lower().encode()).hexdigest()[:16]
                if not title or key in seen:
                    continue
                seen.add(key)
                stories.append(
                    {
                        "source": source,
                        "title": title,
                        "summary": summary[:300],
                        "published": published,
                        "link": link,
                    }
                )
    return stories
    return stories


def fetch_market_snapshot(tickers: list[str]) -> list[dict[str, Any]]:
    snapshot: list[dict[str, Any]] = []
    for ticker in tickers[:20]:
        try:
            obj = yf.Ticker(ticker)
            hist = obj.history(period="5d", interval="1d", auto_adjust=False)
            if hist.empty:
                continue
            latest = float(hist["Close"].iloc[-1])
            prior = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else latest
            pct = ((latest - prior) / prior * 100) if prior else 0
            snapshot.append(
                {"ticker": ticker, "close": round(latest, 2), "daily_change_pct": round(pct, 2)}
            )
        except Exception as exc:
            print(f"Ticker failed: {ticker}: {exc}")
    return snapshot


def build_prompt(stories: list[dict[str, str]], markets: list[dict[str, Any]]) -> str:
    target_words = max(1100, REPORT_MINUTES * 145)
    num_stories = len(stories)
    source_text = "\n".join(
        f"- {s['source']}: {s['title']} | {s['summary']}"
        for s in stories
    )
    market_text = "\n".join(
        f"- {m['ticker']}: {m['close']} ({m['daily_change_pct']:+.2f}%)"
        for m in markets
    )

    return f"""Create a spoken morning briefing of EXACTLY {target_words} words (approximately {REPORT_MINUTES} minutes when read aloud) for Kat, a software engineer who wants to understand what's happening in the world.

CRITICAL LENGTH REQUIREMENT:
- The briefing MUST be at least {target_words} words. Do not cut it short.
- You have {num_stories} news stories to work with.
- If there are fewer stories, go DEEPER on each one. Provide more background, context, history, and analysis.
- Fill the full {REPORT_MINUTES} minutes by being more thorough and educational, not by rushing through.

DATE: {datetime.now(timezone.utc).strftime("%A, %B %d, %Y")} UTC

RULES:
- Only use facts from the sources below. Never invent new events.
- You MAY add well-known background/context/history to explain stories more deeply.
- Attribute sources aloud ("Reuters reports...", "according to the BBC...").
- No URLs, markdown, or bullet points in output.
- Natural spoken transitions between sections.

FOR EACH MAJOR STORY, EXPLAIN THOROUGHLY:
- WHAT happened (the facts)
- BACKGROUND (what led to this, historical context)
- WHY it matters (consequences, who's affected)
- CONNECTIONS (how it relates to other stories, markets, or geopolitics)
- WHAT to watch next

STRUCTURE:

1. OPENING
"Good morning, Kat. Today is [date]. Here are the most important developments shaping the world today."

2. WORLD NEWS (main focus - spend 60% of time here)
Cover by region, but only regions with significant news:

UNITED STATES: Politics, policy, legal developments with real consequences. Skip partisan noise.

EUROPE: Ukraine/Russia war updates, EU policy, NATO, UK. Explain the context and stakes.

MIDDLE EAST: Israel/Palestine, Iran, Gulf states, Red Sea shipping. Explain how conflicts affect oil, shipping, inflation.

ASIA:
- China: Economy, politics, military activity, US-China relations
- Taiwan: Cross-strait tensions, TSMC, semiconductors, elections
- Japan, South Korea, India when significant

LATIN AMERICA & AFRICA: Major developments (coups, elections, crises).

Go deep on the most important stories. Explain like I'm smart but new to the topic.

3. MARKETS & ECONOMY
- S&P 500, Nasdaq, Dow, Treasury yields, oil, gold, bitcoin
- Don't just say "markets rose" - explain WHY they moved
- Connect market moves to the news stories above
- Fed policy, inflation, employment news if relevant

4. TECH & AI
- Major AI developments, cybersecurity, semiconductors
- Explain implications for software engineers and the tech industry

5. QUOTE OF THE DAY
End with one thoughtful, inspiring, or thought-provoking quote from history, philosophy, business, or science. Connect it to today's themes if possible.

Tone: Calm, analytical, educational. Help me understand the world deeply, not just hear headlines.

Remember: You MUST produce {target_words} words. If news is light, go deeper on analysis and background.

NEWS ({num_stories} stories):
{source_text}

MARKETS:
{market_text}
""".strip()


def generate_transcript(stories: list[dict[str, str]], markets: list[dict[str, Any]]) -> str:
    prompt = build_prompt(stories, markets)

    if LLM_PROVIDER == "groq":
        if groq_client is None:
            raise HTTPException(500, "GROQ_API_KEY is not configured.")
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=8192,
            temperature=0.7,
        )
        transcript = response.choices[0].message.content.strip()
    else:
        if openai_client is None:
            raise HTTPException(500, "OPENAI_API_KEY is not configured.")
        response = openai_client.responses.create(
            model=TEXT_MODEL,
            input=prompt,
        )
        transcript = response.output_text.strip()

    if not transcript:
        raise RuntimeError("LLM returned an empty transcript.")
    return transcript


def synthesize_audio(transcript: str, output_path: Path) -> None:
    if TTS_PROVIDER == "edge":
        # Use free Microsoft Edge TTS
        async def _generate():
            communicate = edge_tts.Communicate(transcript, EDGE_VOICE, rate=EDGE_RATE)
            await communicate.save(str(output_path))

        asyncio.run(_generate())
    else:
        # Use OpenAI TTS
        if openai_client is None:
            raise HTTPException(500, "OPENAI_API_KEY is not configured.")
        instructions = (
            "Speak as a calm, precise public-radio news presenter. Use a natural pace, "
            "brief pauses between sections, restrained emotion, and clear pronunciation "
            "of names, countries, numbers, and market symbols."
        )
        with openai_client.audio.speech.with_streaming_response.create(
            model=TTS_MODEL,
            voice=VOICE,
            input=transcript,
            instructions=instructions,
            response_format="mp3",
        ) as response:
            response.stream_to_file(output_path)


def file_paths(date_key: str) -> tuple[Path, Path]:
    return DATA_DIR / f"briefing-{date_key}.mp3", DATA_DIR / f"briefing-{date_key}.txt"


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return """
    <h1>Morning Intelligence Briefing</h1>
    <p>Use <code>/generate?token=YOUR_TOKEN</code> to generate today's briefing.</p>
    <p>Use <code>/audio/today?token=YOUR_TOKEN</code> to play today's audio.</p>
    """


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "llm_provider": LLM_PROVIDER,
        "tts_provider": TTS_PROVIDER,
        "openai_configured": bool(OPENAI_API_KEY),
        "groq_configured": bool(GROQ_API_KEY),
        "access_token_configured": bool(ACCESS_TOKEN),
        "timezone": TIMEZONE_NAME,
    }


@app.get("/generate", response_model=GenerateResponse)
def generate(
    token: str | None = Query(default=None),
    force: bool = Query(default=False),
    authorization: str | None = Header(default=None),
) -> GenerateResponse:
    require_token(token, authorization)
    date_key = datetime.now().strftime("%Y-%m-%d")
    audio_path, transcript_path = file_paths(date_key)
    cached = audio_path.exists() and transcript_path.exists() and not force

    if not cached:
        stories = fetch_feeds()
        if len(stories) < 8:
            raise HTTPException(503, f"Too few news records were available ({len(stories)}).")
        tickers = [t.strip() for t in os.getenv("TICKERS", ",".join(DEFAULT_TICKERS)).split(",") if t.strip()]
        markets = fetch_market_snapshot(tickers)
        transcript = generate_transcript(stories, markets)
        transcript_path.write_text(transcript, encoding="utf-8")
        synthesize_audio(transcript, audio_path)

    return GenerateResponse(
        date=date_key,
        audio_url=f"/audio/{date_key}?token={token or ACCESS_TOKEN}",
        transcript_url=f"/transcript/{date_key}?token={token or ACCESS_TOKEN}",
        cached=cached,
    )


@app.get("/audio/today")
def audio_today(
    token: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
):
    require_token(token, authorization)
    date_key = datetime.now().strftime("%Y-%m-%d")
    audio_path, _ = file_paths(date_key)
    if not audio_path.exists():
        # Generate synchronously so the iPhone shortcut only needs one URL.
        generate(token=token, force=False, authorization=authorization)
    return FileResponse(audio_path, media_type="audio/mpeg", filename=f"briefing-{date_key}.mp3")


@app.get("/audio/{date_key}")
def audio(
    date_key: str,
    token: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
):
    require_token(token, authorization)
    audio_path, _ = file_paths(date_key)
    if not audio_path.exists():
        raise HTTPException(404, "Briefing not found.")
    return FileResponse(audio_path, media_type="audio/mpeg", filename=audio_path.name)


@app.get("/transcript/{date_key}")
def transcript(
    date_key: str,
    token: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
):
    require_token(token, authorization)
    _, transcript_path = file_paths(date_key)
    if not transcript_path.exists():
        raise HTTPException(404, "Transcript not found.")
    return FileResponse(transcript_path, media_type="text/plain", filename=transcript_path.name)


@app.exception_handler(Exception)
async def unhandled_exception_handler(_, exc: Exception):
    print(f"Unhandled error: {exc}")
    return JSONResponse(status_code=500, content={"detail": str(exc)})
