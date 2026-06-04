# Deployment Notes

## Local Runtime

This project currently runs as a long-lived local Python server.

```powershell
python run.py
```

The background paper-trading worker stays alive only while the Python process is running. It stores paper-trading state in local JSON under `data/runs/`.

## Vercel Fit

The current app is not a good direct fit for Vercel production deployment because it depends on:

- a long-running background loop
- local JSON state persistence
- 5-minute decision checks
- 30-minute trading cooldowns
- 6-hour symbol rotation

Vercel Functions are request-driven serverless functions. Vercel Cron Jobs invoke functions on a schedule, but persistent local files are not a durable database. Hobby Cron scheduling is also limited to daily runs, so the current 5-minute loop needs a different architecture.

## Vercel-Compatible Path

To deploy this properly on Vercel:

1. Move state from local JSON to a durable store such as Vercel Postgres, Supabase, Neon, or Upstash Redis.
2. Replace the local background worker with Vercel Cron or an external scheduler.
3. Split the current Python server into serverless API endpoints.
4. Keep the static dashboard as the frontend.
5. Run the 5-minute loop only on a plan/scheduler that supports that frequency.

Until then, local execution is the reliable runtime.
