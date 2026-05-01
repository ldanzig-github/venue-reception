# Venue Reception Dashboard

Standalone live dashboard tracking Google Maps + Tripadvisor reception for **Poolhouse London, Ballers Philadelphia, Ballers Boston Seaport, Five Iron Golf Dubai**. Headless Playwright scrapes every 30 minutes; Flask serves a self-contained HTML page with a 5-minute auto-reload.

No external services, no API keys. Runs on any Linux box with Python 3.10+.

## Files

| Path | Purpose |
|---|---|
| `scraper.py` | Playwright scraper — six page loads, returns dict |
| `renderer.py` | HTML renderer — pure string templating |
| `server.py` | Flask app + APScheduler periodic job |
| `requirements.txt` | Python deps |
| `.env.example` | Config template — copy to `.env` and fill in |
| `deploy/venue-dashboard.service` | systemd unit |
| `deploy/nginx-snippet.conf` | Optional reverse-proxy template |

## Local dev

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium
cp .env.example .env
# Edit .env: at minimum set VENUE_RECEPTION_PATH_TOKEN
python server.py
```

Visit `http://localhost:8090/dashboard/<your-path-token>`.

## VPS deploy (Ubuntu)

```bash
# 1. Clone into /opt
sudo mkdir -p /opt/venue-reception && sudo chown $USER /opt/venue-reception
git clone https://github.com/ldanzig-github/venue-reception.git /opt/venue-reception
cd /opt/venue-reception

# 2. Set up venv + deps
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium

# 3. Configure
cp .env.example .env
# Generate creds:
echo "VENUE_RECEPTION_PATH_TOKEN=$(openssl rand -hex 16)" >> .env
echo "VENUE_RECEPTION_BASIC_AUTH=lloyd:$(openssl rand -hex 24)" >> .env
chmod 600 .env

# 4. Install systemd unit
sudo cp deploy/venue-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now venue-dashboard
sudo systemctl status venue-dashboard

# 5. (Optional) reverse proxy via nginx — see deploy/nginx-snippet.conf

# 6. Read your generated creds back
grep VENUE_RECEPTION .env
```

Visit:
- Local on the VPS: `curl -u lloyd:<pass> http://localhost:8090/dashboard/<token>`
- Through nginx: `https://your-subdomain.example.com/dashboard/<token>`

The first scrape kicks off in a background thread on service start — give it ~60 seconds before the dashboard returns content. Until then `/dashboard/<token>` returns a 503 with a friendly message.

## Operations

- **Logs:** `sudo journalctl -u venue-dashboard -f`
- **Restart:** `sudo systemctl restart venue-dashboard`
- **Tweak interval:** edit `SCRAPE_INTERVAL_MINUTES` in `.env`, then restart
- **Force scrape now:** `sudo systemctl restart venue-dashboard` (kicks the immediate-run thread)
- **Verify HTML on disk:** `ls -la data/venue-reception.html`

## Privacy

The dashboard URL is unguessable (`/dashboard/<32-hex-char-token>`) and gated by HTTP basic auth. The page sends `X-Robots-Tag: noindex, nofollow, noarchive`. The Flask app's catch-all at `/` returns just `ok` — no route enumeration. No links from anywhere else.

## Schema (for adding venues)

`scrape_all_venues()` returns:

```python
{
  "last_scrape": "May 1, 2026 · 2:00 AM",
  "venues": {
    "<key>": {
      "google":     {"rating": "4.7", "count": "95"},
      "trip":       {"rating": "5.0", "count": "4", "rank": "#290 of 1,007"},
      "opentable":  {"rating": "4.5", "count": "30"},
      "distribution": [82, 6, 4, 2, 1],
      "reviews":    [{"source": "g", "rating": 5, "body": "...", "name": "...", "when": "...", "url": "..."}, ...],
      "insight":    "..."
    }
  }
}
```

Add new venues by extending `VENUE_TARGETS` in `scraper.py` (the URL list) and `VENUE_META` in `renderer.py` (display metadata).
