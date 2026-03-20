import type { Express } from "express";
import { createServer, type Server } from "http";
import { storage } from "./storage";
import { insertVaultLogSchema, insertBotSettingsSchema, insertCommandLogSchema } from "@shared/schema";

export async function registerRoutes(
  httpServer: Server,
  app: Express
): Promise<Server> {

  app.get("/api/stats", async (req, res) => {
    try {
      const stats = await storage.getStats();
      res.json(stats);
    } catch (e) {
      res.status(500).json({ error: "Failed to fetch stats" });
    }
  });

  app.get("/api/vault-logs", async (req, res) => {
    try {
      const { type, limit } = req.query;
      const logs = await storage.getVaultLogs(
        undefined,
        type as string | undefined,
        limit ? parseInt(limit as string) : 100
      );
      res.json(logs);
    } catch (e) {
      res.status(500).json({ error: "Failed to fetch vault logs" });
    }
  });

  app.post("/api/vault-logs", async (req, res) => {
    try {
      const data = insertVaultLogSchema.parse(req.body);
      const log = await storage.insertVaultLog(data);
      res.status(201).json(log);
    } catch (e) {
      res.status(400).json({ error: "Invalid data" });
    }
  });

  app.delete("/api/vault-logs/:id", async (req, res) => {
    try {
      await storage.deleteVaultLog(req.params.id);
      res.status(204).send();
    } catch (e) {
      res.status(500).json({ error: "Failed to delete log" });
    }
  });

  app.delete("/api/vault-logs", async (req, res) => {
    try {
      await storage.clearVaultLogs();
      res.status(204).send();
    } catch (e) {
      res.status(500).json({ error: "Failed to clear logs" });
    }
  });

  app.get("/api/settings", async (req, res) => {
    try {
      const settings = await storage.getAllBotSettings();
      res.json(settings);
    } catch (e) {
      res.status(500).json({ error: "Failed to fetch settings" });
    }
  });

  app.put("/api/settings", async (req, res) => {
    try {
      const data = insertBotSettingsSchema.parse(req.body);
      const settings = await storage.upsertBotSettings(data);
      res.json(settings);
    } catch (e) {
      res.status(400).json({ error: "Invalid settings data" });
    }
  });

  app.get("/api/command-logs", async (req, res) => {
    try {
      const { limit } = req.query;
      const logs = await storage.getCommandLogs(
        undefined,
        limit ? parseInt(limit as string) : 50
      );
      res.json(logs);
    } catch (e) {
      res.status(500).json({ error: "Failed to fetch command logs" });
    }
  });

  app.post("/api/command-logs", async (req, res) => {
    try {
      const data = insertCommandLogSchema.parse(req.body);
      const log = await storage.insertCommandLog(data);
      res.status(201).json(log);
    } catch (e) {
      res.status(400).json({ error: "Invalid data" });
    }
  });

  return httpServer;
}
