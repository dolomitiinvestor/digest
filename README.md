# HN Digest

Daily email of Hacker News stories matching your interests, summarized by Claude Haiku.
Runs on GitHub Actions cron. No server.

## Pipeline

```
Algolia HN API     ~120 stories from last 24h, points >= 40      free
      ↓
prefilter          keyword + domain blocklist                    free, instant
      ↓
Claude relevance   ONE call scoring every title 0-10             ~$0.002/day
      ↓            (drops to ~10 stories)
Jina Reader        article text for survivors only               free
Firebase API       top 5 HN comments per story                   free
      ↓
Claude summary     one call per story, 4 in parallel             ~$0.05/day
      ↓
Resend             HTML email                                    free (3k/mo)
```

**Cost: ~$1.60/month** at 10 stories/day. The two-stage filter is the whole trick — you
pay cheap-per-title to decide what to read, expensive-per-article only on the survivors.

## Setup (~15 min)

### 1. Local test first

```bash
git clone <your-repo> && cd hn-digest
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
python hn_digest.py --no-llm --dry-run   # free: check filters, no API calls
open preview.html
python hn_digest.py --dry-run            # real summaries, still no email
```

Iterate on `config.yaml` until `preview.html` looks right. Do this **before** wiring up cron.

### 2. Keys

- **Anthropic**: console.anthropic.com → API Keys. Set a $5 monthly spend limit.
- **Resend**: resend.com → API Keys. Free tier is 3,000 emails/mo, 100/day.
  - Testing: set `MAIL_FROM=onboarding@resend.dev`, works immediately but only sends
    to the address you signed up with.
  - Real use: add a domain, set 3 DNS records, then `MAIL_FROM=digest@yourdomain.com`.

### 3. Repo secrets

GitHub repo → Settings → Secrets and variables → Actions → New repository secret:

| Name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | `sk-ant-...` |
| `RESEND_API_KEY` | `re_...` |
| `MAIL_TO` | your email (comma-separated for several) |
| `MAIL_FROM` | `digest@yourdomain.com` |

Push, then Actions tab → HN Digest → **Run workflow** to trigger it manually. Confirm the
email lands before trusting the schedule.

## Tuning the filter

| Symptom | Fix in `config.yaml` |
|---|---|
| Too many irrelevant stories | `relevance_threshold: 8`, add specifics to `anti_interests` |
| Too few stories | `relevance_threshold: 6`, lower `min_points` to 25 |
| Missing a topic you care about | add to `always_include_keywords` (bypasses the scorer) |
| Summaries too thin | `summary_sentences: 5` |
| Email too long | `max_stories: 6` |

The scorer is only as good as `interests`. Vague categories ("AI", "startups") score
everything a 6. Name the specific thing you want and the thing you don't:
*"inference cost and latency work, NOT funding rounds."*

## Gotchas

- **Cron is UTC and ignores DST.** Your 8am ET digest becomes 7am ET in November.
  Edit the workflow twice a year, or just accept the hour.
- **GitHub disables scheduled workflows after 60 days of repo inactivity.** It emails you
  first. A single commit resets it.
- **Jina Reader** fails on paywalls, PDFs, and videos — the script falls back to
  summarizing from HN comments alone, which is often fine.
- **No dedupe across runs.** A 24h lookback with a 24h cron means near-zero overlap. If
  you go twice-daily, set `lookback_hours: 12`.
- **Free-tier usage**: ~2 min/run × 22 runs = ~45 of your 2,000 monthly Actions minutes.
  Private repos count minutes; public repos are unlimited.
