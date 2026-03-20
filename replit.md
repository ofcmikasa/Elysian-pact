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
