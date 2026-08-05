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
REPORT_MINUTES = int(os.getenv("REPORT_MINUTES", "12"))

# Provider selection: "openai" or "groq" for LLM, "openai" or "edge" for TTS
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "groq").lower()
TTS_PROVIDER = os.getenv("TTS_PROVIDER", "edge").lower()

VOICE = os.getenv("VOICE", "coral")  # OpenAI voice
EDGE_VOICE = os.getenv("EDGE_VOICE", "en-US-JennyNeural")  # Edge TTS voice
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


def fetch_feeds(max_per_feed: int = 4, max_total: int = 40) -> list[dict[str, str]]:
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
    source_text = "\n".join(
        f"- SOURCE: {s['source']} | TITLE: {s['title']} | PUBLISHED: {s['published']} "
        f"| SUMMARY: {s['summary']} | LINK: {s['link']}"
        for s in stories
    )
    market_text = "\n".join(
        f"- {m['ticker']}: {m['close']} ({m['daily_change_pct']:+.2f}% latest session)"
        for m in markets
    )

    return f"""
Create a spoken morning intelligence briefing of about {target_words} words for a curious,
educated listener who wants to understand the world rather than merely hear headlines.

DATE: {datetime.now(timezone.utc).strftime("%B %d, %Y")} UTC
TIMEZONE CONTEXT: {TIMEZONE_NAME}

NON-NEGOTIABLE ACCURACY RULES:
1. Use only facts supported by the supplied source records and market snapshot.
2. Never invent a quotation, number, cause, diplomatic development, market move, or event.
3. If records conflict or are unclear, say that reporting is incomplete or differs.
4. Attribute important facts aloud, using phrases such as "Reuters reports" or
   "according to the BBC." Google News is only the delivery mechanism; attribute the
   underlying publisher named in the source label or headline.
5. Distinguish confirmed facts from analysis and reasonable inference.
6. Avoid sensational language and false certainty.
7. Do not give individualized investment advice.
8. Do not read URLs aloud.

STRUCTURE AND PERSONALIZATION:

Begin exactly in this style:
"Good morning, Kat. Today is [weekday], [month] [day]. Today will be [brief weather summary].
Here are the five developments most likely to shape the world today."

Use the actual current date. Do not hard-code the weekday or date. Weather will be of Cupertino, California, unless personal weather data is supplied.

1. EXECUTIVE SUMMARY — approximately 2 minutes

Identify the five developments most likely to shape global affairs, markets, technology,
or the economy today.

For each development:
- State what happened.
- Explain why it matters.
- Explain what could happen next.

Prioritize consequential developments rather than unusual, sensational, or entertaining
headlines.

2. WORLD NEWS — approximately 5 to 6 minutes

Organize this section by region. Only include a region when there is a meaningful,
well-supported development in the supplied source records.

NORTH AMERICA:
- Major United States political, economic, legal, or foreign-policy developments.
- Major developments in Canada and Mexico.
- Exclude routine partisan commentary and minor political disputes.
- Focus on events with significant domestic or international consequences.
- Add brief mention of California when relevant to the national economy, politics, or markets.

EUROPE:
- Ukraine and Russia.
- European Union policy.
- NATO.
- United Kingdom.
- Elections, economic policy, security, energy, and diplomacy.

For each important European story, explain:
- What happened?
- What caused or preceded it?
- Why does it matter?
- Who is affected?
- What should the listener watch next?

MIDDLE EAST:
- Israel and the Palestinian territories.
- Iran.
- Saudi Arabia.
- Syria.
- Lebanon.
- Gulf states.
- Red Sea security when relevant.

Explain:
- Military developments.
- Diplomatic negotiations.
- Regional alliances.
- Humanitarian effects.
- Oil, gas, shipping, inflation, and market implications.

ASIA:
Give additional attention to:
- China.
- Taiwan.
- Japan.
- South Korea.
- India.

For Taiwan, include meaningful developments involving:
- Cross-Strait relations.
- Chinese military activity.
- Taiwanese elections and government policy.
- United States-Taiwan relations.
- Semiconductors.
- TSMC.
- Supply-chain security.
- Taiwan's economy.

Do not force Taiwan coverage when there is no important supported development.

LATIN AMERICA:
Include only major developments involving:
- Elections.
- Government instability.
- Economic crises.
- Security.
- Migration.
- Trade.
- Climate and natural disasters.

AFRICA:
Do not ignore Africa merely because fewer stories appear in Western media.

Prioritize:
- Elections.
- Coups and political instability.
- Armed conflicts.
- Humanitarian crises.
- Public health.
- Economic development.
- Infrastructure.
- Regional diplomacy.
- Climate and food security.

Do not provide superficial regional roundups. Explain only the most consequential
well-supported stories.

3. MARKETS AND GLOBAL ECONOMY — approximately 3 to 4 minutes

Do not merely report whether an asset rose or fell.

Explain why markets moved and what investors were responding to.

Discuss when supported by the available data:
- S&P 500.
- Nasdaq Composite.
- Dow Jones Industrial Average.
- Russell 2000.
- United States Treasury yields.
- United States dollar.
- Oil.
- Gold.
- Bitcoin.

Also explain relevant developments involving:
- Federal Reserve policy.
- Inflation.
- Employment.
- Unemployment.
- Wages.
- GDP.
- Consumer spending.
- Manufacturing.
- Interest-rate expectations.
- Major international central banks.
- Important economic data releases.

Clearly distinguish:
- Confirmed explanations reported by credible sources.
- Market commentary.
- Your own cautious interpretation.

Never claim that one factor definitively caused a market move unless the supplied reporting
supports that conclusion.

4. BUSINESS AND COMPANIES — approximately 2 to 3 minutes

Focus only on companies and transactions that materially affect the economy, markets,
technology, or major industries.

Give particular attention when relevant to:
- Apple.
- Microsoft.
- Nvidia.
- Tesla.
- Amazon.
- Meta.
- Alphabet and Google.
- TSMC.
- AMD.
- OpenAI, but only when supported by credible public reporting.
- Major mergers.
- Major acquisitions.
- Important IPOs.
- Significant earnings reports.
- Antitrust cases.
- Regulatory actions.
- Major layoffs or capital-investment announcements.

Do not summarize every earnings report. Select the developments with the greatest broader
significance.

For each selected company story, explain:
- What happened.
- Why investors, workers, consumers, or governments care.
- What it reveals about the wider industry.

5. AI AND TECHNOLOGY — approximately 2 minutes

Include important developments involving:
- Artificial intelligence models.
- AI infrastructure.
- Robotics.
- Software.
- Developer tools.
- Open-source projects.
- Cybersecurity.
- Semiconductors.
- Quantum computing.
- Space technology.
- Technology regulation.

Explain why each development matters for the future of:
- Work.
- Software engineering.
- Business.
- National security.
- Productivity.
- Competition.
- Privacy.
- Society.

Avoid repeating company news already covered unless additional technical explanation is
useful.

6. ENGINEERING AND AI — approximately 1 to 2 minutes

Create a specialized section for a software engineer.

Prioritize:
- Major software releases.
- Important open-source projects.
- Developer tools.
- Cloud infrastructure.
- Programming languages.
- Cybersecurity vulnerabilities.
- Significant engineering research.
- AI research papers with practical importance.
- Changes that could affect software engineering work.

Explain practical relevance rather than listing announcements.

7. SCIENCE — approximately 1 minute

Select one to three credible and consequential developments from:
- Medicine.
- Public health.
- Astronomy.
- Climate science.
- Biology.
- Archaeology.
- Physics.
- Environmental science.

Explain:
- What researchers found.
- How strong the evidence is.
- Why the finding matters.
- What remains uncertain.

Do not exaggerate preliminary research.

8. INVESTING CORNER — approximately 2 minutes

Teach one durable investing or economics concept connected to the day's news.

Possible topics include:
- Why bond yields matter.
- Why a stock can fall after good earnings.
- Currency carry trades.
- How oil prices affect inflation.
- Valuation multiples.
- Market expectations.
- Interest-rate sensitivity.
- Diversification.
- Credit spreads.
- Yield curves.
- Currency risk.
- Economic cycles.

Do not recommend specific securities or tell the listener what to buy or sell.

Explain the concept in plain English using the day's events as an example.

9. TODAY'S INTERESTING FACT — approximately 30 seconds

Provide one accurate, genuinely educational fact involving:
- History.
- Economics.
- Geography.
- Psychology.
- Science.
- Culture.

Prefer a fact that connects naturally to one of the day's major stories.

Do not invent or repeat an unsupported fact. Only include this section when the supplied
sources support it or when the fact is stable, widely established general knowledge.

10. DAILY DEEP DIVE — approximately 3 to 5 minutes

Choose one topic that helps the listener build a stronger long-term understanding of global
affairs, economics, technology, or history.

Possible topics include:
- What NATO is and how it works.
- Why Taiwan is central to semiconductor production.
- How tariffs work.
- Why the Strait of Hormuz matters.
- Who the Houthis are.
- What caused Japan's lost decades.
- How central banks influence inflation.
- How semiconductor supply chains work.
- Why Treasury yields affect technology stocks.
- How sanctions work.
- How elections are structured in a relevant country.

Connect the deep dive to a current development whenever possible.

Cover:
- Essential background.
- Key institutions or actors.
- Historical development.
- Current relevance.
- Common misconceptions.
- What to watch next.

Do not choose a deep-dive topic unless the supplied reporting or stable general knowledge
supports an accurate explanation.

Do not fabricate personal information.

Do not provide personalized investment recommendations.

11. DAY-SPECIFIC SECTIONS

MONDAYS:
Add a short weekly economic calendar covering major scheduled events such as:
- Central-bank meetings.
- Inflation releases.
- Employment reports.
- GDP.
- Major earnings.
- Elections.
- Important diplomatic meetings.

FRIDAYS:
Add a short markets week-in-review covering:
- Major index performance.
- Biggest meaningful winners and losers.
- The main forces that moved markets.
- Important lessons from the week.

SUNDAYS:
Produce a longer week-in-review that:
- Summarizes the week's most consequential developments.
- Separates short-term headlines from long-term trends.
- Explains how major events connect.
- Identifies what to watch during the coming week.

12. CONNECTIONS AND MENTAL MODELS

Throughout the briefing, explicitly connect related developments.

Examples:
- How geopolitics affects energy prices.
- How energy prices affect inflation.
- How inflation affects central-bank policy.
- How interest rates affect technology valuations.
- How AI investment affects semiconductor demand.
- How semiconductor demand affects Taiwan.
- How wars affect trade, currencies, food, migration, and government budgets.
- How regulation affects technology companies and markets.

The listener should understand systems and causal relationships, not merely memorize
isolated headlines.

13. CLOSING

End with:
- Five developments to watch during the next 24 hours.
- One sentence summarizing the central theme connecting today's major stories.
- One quote of the day that is inspirational, self help.

Use natural spoken transitions.

Do not use markdown, tables, bullet symbols, citation brackets, section numbers, or URLs
in the final narration.

Clearly announce each major section in natural speech, such as:
"Now to Europe."
"Turning to markets."
"Next, today's investing lesson."
"Finally, here are five things to watch."

Keep the tone calm, analytical, concise, educational, and credible.
Make transitions natural for audio. No markdown, tables, bullets, or citation brackets.

SOURCE RECORDS:
{source_text}

MARKET SNAPSHOT (unofficial delayed public-market data; describe as the latest available
session, not necessarily live):
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
            max_tokens=8000,
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
            communicate = edge_tts.Communicate(transcript, EDGE_VOICE)
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
