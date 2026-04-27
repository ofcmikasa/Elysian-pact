# Elysian Dashboard

A web dashboard for the **Elysian** Discord bot — a mystical library guardian bot.

## Overview

This is a React + Express fullstack app that serves as the admin dashboard for the Elysian Discord bot. It provides visibility into vault logs (deleted/edited messages) and bot configuration.

## Architecture

- **Frontend**: React + TypeScript + Vite, using shadcn/ui components and TailwindCSS
- **Backend**: Express.js with in-memory storage (MemStorage)
- **State Management**: TanStack Query for data fetching
- **Routing**: wouter

## Pages

- `/` — Overview dashboard with stats and recent activity
- `/vault` — Full vault log viewer (deleted/edited messages) with filtering and deletion
- `/commands` — Command documentation and execution history
- `/settings` — Bot configuration settings

## API Routes

- `GET /api/stats` — Summary statistics
- `GET /api/vault-logs` — All vault log entries (filterable by type)
- `POST /api/vault-logs` — Add a new vault log
- `DELETE /api/vault-logs/:id` — Remove a specific log
- `DELETE /api/vault-logs` — Clear all logs
- `GET /api/settings` — Bot settings
- `PUT /api/settings` — Update bot settings
- `GET /api/command-logs` — Command execution history
- `POST /api/command-logs` — Add a command log

## Theme

Deep purple/violet primary color with dark navy backgrounds. Light mode also supported. Features a "Library Guardian" aesthetic matching the Elysian bot's identity.

## Run

```bash
npm run dev
```

Serves on port 5000.

## Bot (bot.py)

Runs on Render (live: elysian-pact.onrender.com). 51 slash commands across:
- **Profile/Economy** — /profile (with Comeback Card overlay), /leaderboard, /daily, /shop
- **Study/Focus** — /focus, /endfocus (x3 Final Stand multiplier, burnout lock at 180 min/day, raid damage), /pomodoro, /deepwork, /task, /post_resource
- **High-Stakes** — /vow (Ink Pact w/ ante + deadline + daily goal), /end_vow, /shame_board, /gambit (bet → x3), /quit_gambit, /duel (PvP trivia), /raid_status
- **Oracle (AI)** — /ask, /summarize, /oracle_challenge (Socratic 3-turn), /simplify (ELI5), /critique (harsh prof), /quiz_me (5-Q one-shot, Sage badge for perfect / -50 Ink for 0), /ledger (weakness heat map), DM chat (3 personas)
- **Admin** — /elysian_genesis, /broadcast, /embed, welcome/goodbye, templates, /set_ink, /admin_add_item, /raid_start (owner)
- **Moderation** — /mute, /warn, /warnings, /kick, /ban, /purge, /purge_user, /nuke, /slowmode, /lock, /unlock, /lockdown_server, /vault_view

Background loops: vow_morning_check (10:00 UTC nudges) and vow_midnight_check (burns Ink for missed daily goal, posts public shame, expires vows + raids).

Data files: users.json, sessions.json, shop.json, guild_config.json, vault.json, templates.json, vows.json, ledger.json, raid.json.
