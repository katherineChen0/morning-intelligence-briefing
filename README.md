# Morning Intelligence Briefing

This package creates a personalized, spoken world-news and stock-market briefing and
lets an iPhone play it automatically when an alarm is stopped.

## What is already built

- Fetches current headlines from a fixed, editable source allowlist.
- Includes targeted Reuters and Associated Press results delivered through Google News RSS.
- Pulls a delayed latest-session market snapshot for major indexes and selected stocks.
- Uses OpenAI to synthesize an explanatory report with explicit source attribution.
- Uses OpenAI text-to-speech to produce an MP3.
- Caches one report per calendar day.
- Protects the endpoint with a secret access token.
- Includes local Mac startup scripts, Docker, and a Render deployment blueprint.

## Important limitation

An iPhone cannot reach a server running only on a sleeping or closed laptop. For reliable
morning playback, deploy this app to an always-on host. Render is the easiest included
option. A paid always-on instance is more reliable than a service that sleeps when idle.

You must supply your own OpenAI API key and pay the resulting API usage. API keys cannot
safely be bundled into a downloadable project.

## Recommended setup: deploy to Render

1. Unzip this folder on your personal Mac.
2. Create a private GitHub repository and upload all files in this folder.
3. Sign in to Render and create a **Blueprint** from that repository.
4. Render detects `render.yaml`.
5. In Render's environment settings, paste your `OPENAI_API_KEY`.
6. Render automatically creates `ACCESS_TOKEN`. Copy its value from the environment page.
7. After deployment, open:
   `https://YOUR-SERVICE.onrender.com/health`
8. Test:
   `https://YOUR-SERVICE.onrender.com/audio/today?token=YOUR_ACCESS_TOKEN`

The first request of each day may take long enough that iOS playback is delayed. For the
smoothest result, create a second scheduled request 10–15 minutes before your normal alarm,
as described below.

## iPhone Shortcut: generate and play the briefing

Create a regular Shortcut named **Play Morning Briefing**:

1. Open **Shortcuts** → **Shortcuts** → `+`.
2. Add **URL**.
3. Enter:
   `https://YOUR-SERVICE.onrender.com/audio/today?token=YOUR_ACCESS_TOKEN`
4. Add **Get Contents of URL**.
5. Add **Set Volume** and choose a comfortable level, such as 45%.
6. Add **Play Sound**. Set its input to the result of **Get Contents of URL**.
7. Optionally add **Set Playback Destination** and select a HomePod or other available
   AirPlay target. This can be less reliable if the target is asleep or unavailable.
8. Name and test the shortcut while the phone is unlocked.

## iPhone Automation: run when the alarm stops

1. Open **Shortcuts** → **Automation** → `+`.
2. Choose **Alarm**.
3. Select **Goes Off** for playback as the alarm starts, or **Is Stopped** to play after
   you dismiss it. `Is Stopped` is usually less disruptive.
4. Select the specific wake-up alarm, or **Any**.
5. Choose **Run Immediately** / disable **Ask Before Running**, depending on the wording
   shown by your iOS version.
6. Add the **Run Shortcut** action.
7. Select **Play Morning Briefing**.
8. Save and perform one real-alarm test.

iOS behavior can vary with lock state, Focus mode, Bluetooth/AirPlay availability, and
system updates. Test the complete automation before relying on it as your only alarm.

## Optional pre-generation automation

To avoid waiting for the report to be created:

1. Make a Shortcut called **Prepare Morning Briefing**.
2. Add a URL action:
   `https://YOUR-SERVICE.onrender.com/generate?token=YOUR_ACCESS_TOKEN`
3. Add **Get Contents of URL**.
4. Create a personal **Time of Day** automation for 10–15 minutes before your alarm.
5. Set it to run immediately.

Then the alarm-triggered Shortcut retrieves the cached MP3 almost immediately.

## Local Mac test

1. Duplicate `.env.example` and rename the copy `.env`.
2. Add your OpenAI API key and choose a long random `ACCESS_TOKEN`.
3. Double-click `start.command`.
4. macOS may require right-click → **Open** the first time.
5. In another browser tab, open:
   `http://127.0.0.1:8000/audio/today?token=YOUR_ACCESS_TOKEN`

To make the scripts executable in Terminal:

```bash
chmod +x start.command test_briefing.command
```

## Customize the report

Edit `.env` or Render environment variables:

- `REPORT_MINUTES=12`
- `VOICE=coral`
- `TICKERS=^GSPC,^DJI,^IXIC,AAPL,MSFT,NVDA`
- `TEXT_MODEL=gpt-5-mini`
- `TTS_MODEL=gpt-4o-mini-tts`

Edit `RSS_FEEDS` and the prompt inside `app.py` to change sources, sections, style, or
regional emphasis.

## Credibility controls

The application does not let the model freely browse. It provides a bounded set of
headline records and tells the model to use only those records, attribute important claims,
flag conflicts, avoid fabricated quotations and numbers, and distinguish reporting from
analysis.

This reduces hallucination risk but does not eliminate it. RSS summaries can be incomplete,
Google News can mislabel or duplicate stories, and the market snapshot is unofficial and
delayed. For consequential decisions, open the original reports and verify them directly.

## Security

- Keep the GitHub repository private.
- Never commit `.env`.
- Use a long random `ACCESS_TOKEN`.
- Rotate the token if the URL is shared.
- Do not place your OpenAI key directly inside an iPhone Shortcut.
