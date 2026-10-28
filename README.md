# Naukri Auto Apply Bot

Automatically searches and applies to jobs on [Naukri.com](https://www.naukri.com) using Playwright browser automation.

## Features

- Logs in to your Naukri account automatically
- Searches across multiple keywords in one run
- Filters jobs by experience range (skips over-qualified postings)
- Applies directly on Naukri (one-click apply)
- Also attempts to apply on external company sites
- GCC / MNC company type filter support
- Fills chatbot questions (CTC, notice period, location, etc.) from your Naukri profile
- Deduplicates — never applies to the same job twice
- Logs every application to `applied_jobs.json`

## Requirements

- Python 3.10+
- Google Chrome / Chromium

## Setup

**1. Clone the repo**
```bash
git clone https://github.com/YOUR_USERNAME/naukri-auto-apply.git
cd naukri-auto-apply
```

**2. Install dependencies**
```bash
pip install -r requirements.txt
playwright install chromium
```
Or on Windows, just double-click `setup.bat`.

**3. Create your config**
```bash
cp config.example.json config.json
```
Then edit `config.json` with your Naukri credentials and preferences.

> **`config.json` is in `.gitignore` — your credentials will never be committed.**

## Configuration

| Field | Description |
|---|---|
| `credentials.email` | Your Naukri login email |
| `credentials.password` | Your Naukri password |
| `search.keywords` | List of keywords to search (runs each one) |
| `search.location` | City filter (e.g. `"Pune"`) |
| `search.experience` | Your years of experience |
| `search.work_mode` | `"hybrid"`, `"work from home"`, `"office"` or comma-separated |
| `search.gcc_only` | `true` = filter for GCC/captive companies only |
| `settings.max_apply_per_run` | Max applications per session |
| `settings.confirm_before_apply` | `false` = fully automatic, `true` = ask before each job |
| `settings.skip_company_site_jobs` | `false` = also apply on external company sites |
| `settings.headless` | `false` = show browser window, `true` = run in background |

## Usage

```bash
# Full auto-apply run
python main.py

# Dry run — search only, no applications sent
python main.py --dry-run

# View summary of all applied jobs
python main.py --show-log

# Custom config file
python main.py --config my_config.json

# Verbose logging
python main.py -v
```

## How it works

1. Logs in to Naukri
2. Fetches your profile (name, phone, CTC, notice period, etc.)
3. Searches each keyword with your filters
4. For each job card: checks experience requirement, skips if too senior
5. Clicks Apply — handles Naukri's chatbot Q&A automatically
6. For company-site jobs: opens the external page and does a best-effort form fill
7. Logs result to `applied_jobs.json`

## Notes

- `applied_jobs.json` tracks every job so you never apply twice (even across runs)
- Company site apply success rate varies — each company uses a different ATS
- If Naukri shows a CAPTCHA, set `"headless": false` and solve it manually once
