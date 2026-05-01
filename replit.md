# Workspace

## Overview

pnpm workspace monorepo using TypeScript. Each package manages its own dependencies.

## Stack

- **Monorepo tool**: pnpm workspaces
- **Node.js version**: 24
- **Package manager**: pnpm
- **TypeScript version**: 5.9
- **API framework**: Express 5
- **Database**: PostgreSQL + Drizzle ORM
- **Validation**: Zod (`zod/v4`), `drizzle-zod`
- **API codegen**: Orval (from OpenAPI spec)
- **Build**: esbuild (CJS bundle)

## Python Web Panel V3

Located in `panel/`. A FastAPI-based process manager and web dashboard with Discord bot and Cloudflare Tunnel support.

- `panel/app.py` — FastAPI main app (routes, WebSockets, upload/delete)
- `panel/manager.py` — Script lifecycle manager (start/stop/restart/watch)
- `panel/tunnel.py` — Cloudflare Tunnel integration (auto-download cloudflared)
- `panel/discord_bot.py` — Discord slash command bot (discord.py v2)
- `panel/templates/` — Jinja2 HTML templates (index, logs, settings)
- `panel/scripts/` — User Python scripts live here
- `panel/logs/` — Per-script and tunnel log files
- `panel/config.json` — Cloudflare + Discord tokens and allowed user IDs

Runs on port 8000. Workflow: `cd /home/runner/workspace/panel && python app.py`

## Key Commands

- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- `pnpm --filter @workspace/api-server run dev` — run API server locally

See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details.
