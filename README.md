# Hathway Broadband Usage Dashboard

This repo fetches Hathway usage data, updates `outputs/broadband_usage.json`, and regenerates `outputs/broadband_usage.html`.

## GitHub Secrets

Add these under **Repo -> Settings -> Secrets and variables -> Actions -> Repository secrets**:

- `HATHWAY_AUTHORIZATION`
- `HATHWAY_ACCOUNT_NO`
- `HATHWAY_REGISTERED_MOBILE_NO`

Use a fresh `HATHWAY_AUTHORIZATION` token from your browser request. Do not commit `outputs/hathway_config.json`.

## Run

Open **Actions -> Hathway Usage -> Run workflow**.

The workflow:

- validates secrets
- fetches usage for `year-to-now`
- commits updated JSON/HTML back to the repo
- uploads the dashboard as a workflow artifact
- deploys to GitHub Pages only if the repo is public

If Hathway returns `403` even with valid secrets, GitHub's cloud runner may be blocked by Hathway. In that case, use a self-hosted runner on your Mac/home network.

## Local Run

```bash
python3 scripts/hathway_usage.py
```

For local credentials, copy `outputs/hathway_config.example.json` to `outputs/hathway_config.json`.

