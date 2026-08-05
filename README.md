End-to-End Setup Guide
Step 1: Get Your Free Groq API Key
Go to https://console.groq.com/keys
Sign up (free)
Create an API key
Copy it
Step 2: Configure .env
cd ~/Downloads/morning-intelligence-briefing

Edit .env and replace:

gsk_your-free-groq-key-here → your Groq API key
replace-with-a-long-random-secret → a random string (e.g., run openssl rand -hex 32)
Step 3: Test Locally
# Install dependencies
pip install -r requirements.txt

# Run the server
./start.command
# Or: uvicorn app:app --reload

Open: http://127.0.0.1:8000/audio/today?token=YOUR_ACCESS_TOKEN

First run takes 2-3 minutes (generates transcript + audio).

Step 4: Deploy to Render (Free)
Create GitHub repo:
cd ~/Downloads/morning-intelligence-briefing
git init
git add .
git commit -m "Initial commit"

Push to a new private GitHub repo

Deploy on Render:

Go to https://render.com → New → Blueprint
Connect your GitHub repo
Render detects render.yaml automatically
In Environment settings, add GROQ_API_KEY (paste your key)
Copy the auto-generated ACCESS_TOKEN
Test deployment:
https://YOUR-SERVICE.onrender.com/health
https://YOUR-SERVICE.onrender.com/audio/today?token=YOUR_ACCESS_TOKEN

Step 5: iPhone Shortcut
Shortcut 1: "Prepare Morning Briefing" (pre-generate)

Shortcuts → + → Add URL: https://YOUR-SERVICE.onrender.com/generate?token=YOUR_TOKEN
Add "Get Contents of URL"
Shortcut 2: "Play Morning Briefing"

Add URL: https://YOUR-SERVICE.onrender.com/audio/today?token=YOUR_TOKEN
Add "Get Contents of URL"
Add "Set Volume" → 45%
Add "Play Sound" (input: Contents of URL)
Step 6: iPhone Automation
Pre-generation (15 min before alarm):

Shortcuts → Automation → + → Time of Day
Set to 15 min before your alarm
Run "Prepare Morning Briefing"
Run Immediately = ON
Play on alarm:

Automation → + → Alarm → Is Stopped
Run "Play Morning Briefing"
Run Immediately = ON
Total cost: $0/month (Groq free tier + Edge TTS + Render free tier)

Note: Free Render sleeps after 15 min idle—that's why the pre-generation shortcut wakes it up before your alarm.

