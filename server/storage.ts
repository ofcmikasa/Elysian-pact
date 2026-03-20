import { type VaultLog, type InsertVaultLog, type BotSettings, type InsertBotSettings, type CommandLog, type InsertCommandLog } from "@shared/schema";
import { randomUUID } from "crypto";

export interface IStorage {
  getVaultLogs(guildId?: string, type?: string, limit?: number): Promise<VaultLog[]>;
  insertVaultLog(log: InsertVaultLog): Promise<VaultLog>;
  deleteVaultLog(id: string): Promise<void>;
  clearVaultLogs(): Promise<void>;

  getBotSettings(guildId: string): Promise<BotSettings | undefined>;
  getAllBotSettings(): Promise<BotSettings[]>;
  upsertBotSettings(settings: InsertBotSettings): Promise<BotSettings>;

  getCommandLogs(guildId?: string, limit?: number): Promise<CommandLog[]>;
  insertCommandLog(log: InsertCommandLog): Promise<CommandLog>;

  getStats(): Promise<{
    totalLogs: number;
    deletedMessages: number;
    editedMessages: number;
    totalCommands: number;
    activeGuilds: number;
  }>;
}

export class MemStorage implements IStorage {
  private vaultLogs: Map<string, VaultLog>;
  private botSettings: Map<string, BotSettings>;
  private commandLogs: Map<string, CommandLog>;

  constructor() {
    this.vaultLogs = new Map();
    this.botSettings = new Map();
    this.commandLogs = new Map();
    this.seed();
  }

  private seed() {
    const guildId = "1234567890123456789";
    const settingsId = randomUUID();
    const settings: BotSettings = {
      id: settingsId,
      guildId,
      guildName: "The Elysian Library",
      vaultChannelId: "1484495219176243200",
      vaultChannelName: "elysian-vault",
      ownerId: "1456572804815261858",
      botPrefix: "e!",
      isActive: true,
      loggingEnabled: true,
    };
    this.botSettings.set(guildId, settings);

    const now = new Date();
    const logsData: Omit<VaultLog, "id">[] = [
      {
        type: "delete",
        authorName: "LunarScholar",
        authorId: "111222333444555666",
        channelName: "general-discourse",
        channelId: "1234567890123456780",
        originalContent: "Has anyone found the restricted section yet? I heard there's a tome on shadow manipulation.",
        revisedContent: null,
        guildName: "The Elysian Library",
        timestamp: new Date(now.getTime() - 1000 * 60 * 5),
      },
      {
        type: "edit",
        authorName: "AstralArchivist",
        authorId: "222333444555666777",
        channelName: "study-hall",
        channelId: "1234567890123456781",
        originalContent: "I think the event starts at 7pm",
        revisedContent: "I think the event starts at 8pm EST, not 7pm. My mistake.",
        guildName: "The Elysian Library",
        timestamp: new Date(now.getTime() - 1000 * 60 * 18),
      },
      {
        type: "delete",
        authorName: "VoidWatcher",
        authorId: "333444555666777888",
        channelName: "announcements",
        channelId: "1234567890123456782",
        originalContent: "The midnight reading session has been cancelled due to unforeseen circumstances.",
        revisedContent: null,
        guildName: "The Elysian Library",
        timestamp: new Date(now.getTime() - 1000 * 60 * 45),
      },
      {
        type: "edit",
        authorName: "CelestialScribe",
        authorId: "444555666777888999",
        channelName: "general-discourse",
        channelId: "1234567890123456780",
        originalContent: "this bot is actually pretty cool ngl",
        revisedContent: "This bot is actually quite impressive! The logging is seamless.",
        guildName: "The Elysian Library",
        timestamp: new Date(now.getTime() - 1000 * 60 * 92),
      },
      {
        type: "delete",
        authorName: "NightSeer",
        authorId: "555666777888999000",
        channelName: "off-topic",
        channelId: "1234567890123456783",
        originalContent: "anyone want to trade grimoires? i have a spare copy of the arcane fundamentals",
        revisedContent: null,
        guildName: "The Elysian Library",
        timestamp: new Date(now.getTime() - 1000 * 60 * 180),
      },
      {
        type: "edit",
        authorName: "EtherealEnvoy",
        authorId: "666777888999000111",
        channelName: "study-hall",
        channelId: "1234567890123456781",
        originalContent: "The thesis on dimensional theory is due friday",
        revisedContent: "The thesis on dimensional theory is due this Friday at midnight. Don't forget to submit to the vault.",
        guildName: "The Elysian Library",
        timestamp: new Date(now.getTime() - 1000 * 60 * 260),
      },
      {
        type: "delete",
        authorName: "SilverSage",
        authorId: "777888999000111222",
        channelName: "general-discourse",
        channelId: "1234567890123456780",
        originalContent: "wait is the admin actually watching everything we say lol",
        revisedContent: null,
        guildName: "The Elysian Library",
        timestamp: new Date(now.getTime() - 1000 * 60 * 340),
      },
    ];

    for (const log of logsData) {
      const id = randomUUID();
      this.vaultLogs.set(id, { ...log, id });
    }

    const commandsData: Omit<CommandLog, "id">[] = [
      {
        commandName: "purge",
        executedBy: "Architect",
        executedById: "1456572804815261858",
        channelName: "general-discourse",
        guildName: "The Elysian Library",
        details: "Purged 15 messages",
        success: true,
        timestamp: new Date(now.getTime() - 1000 * 60 * 30),
      },
      {
        commandName: "lock",
        executedBy: "Architect",
        executedById: "1456572804815261858",
        channelName: "off-topic",
        guildName: "The Elysian Library",
        details: "Channel locked for deep work session",
        success: true,
        timestamp: new Date(now.getTime() - 1000 * 60 * 120),
      },
      {
        commandName: "purge",
        executedBy: "Architect",
        executedById: "1456572804815261858",
        channelName: "announcements",
        guildName: "The Elysian Library",
        details: "Purged 5 messages",
        success: true,
        timestamp: new Date(now.getTime() - 1000 * 60 * 200),
      },
    ];

    for (const cmd of commandsData) {
      const id = randomUUID();
      this.commandLogs.set(id, { ...cmd, id });
    }
  }

  async getVaultLogs(guildId?: string, type?: string, limit = 100): Promise<VaultLog[]> {
    let logs = Array.from(this.vaultLogs.values());
    if (guildId) logs = logs.filter(l => l.guildName !== undefined);
    if (type && type !== "all") logs = logs.filter(l => l.type === type);
    return logs.sort((a, b) => b.timestamp.getTime() - a.timestamp.getTime()).slice(0, limit);
  }

  async insertVaultLog(log: InsertVaultLog): Promise<VaultLog> {
    const id = randomUUID();
    const entry: VaultLog = { ...log, id, timestamp: new Date() };
    this.vaultLogs.set(id, entry);
    return entry;
  }

  async deleteVaultLog(id: string): Promise<void> {
    this.vaultLogs.delete(id);
  }

  async clearVaultLogs(): Promise<void> {
    this.vaultLogs.clear();
  }

  async getBotSettings(guildId: string): Promise<BotSettings | undefined> {
    return this.botSettings.get(guildId);
  }

  async getAllBotSettings(): Promise<BotSettings[]> {
    return Array.from(this.botSettings.values());
  }

  async upsertBotSettings(settings: InsertBotSettings): Promise<BotSettings> {
    this.botSettings.set(settings.guildId, settings as BotSettings);
    return settings as BotSettings;
  }

  async getCommandLogs(guildId?: string, limit = 50): Promise<CommandLog[]> {
    let logs = Array.from(this.commandLogs.values());
    return logs.sort((a, b) => b.timestamp.getTime() - a.timestamp.getTime()).slice(0, limit);
  }

  async insertCommandLog(log: InsertCommandLog): Promise<CommandLog> {
    const id = randomUUID();
    const entry: CommandLog = { ...log, id, timestamp: new Date() };
    this.commandLogs.set(id, entry);
    return entry;
  }

  async getStats() {
    const logs = Array.from(this.vaultLogs.values());
    const cmds = Array.from(this.commandLogs.values());
    return {
      totalLogs: logs.length,
      deletedMessages: logs.filter(l => l.type === "delete").length,
      editedMessages: logs.filter(l => l.type === "edit").length,
      totalCommands: cmds.length,
      activeGuilds: this.botSettings.size,
    };
  }
}

export const storage = new MemStorage();
