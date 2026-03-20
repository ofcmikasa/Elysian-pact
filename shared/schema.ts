import { pgTable, text, varchar, timestamp, boolean, integer } from "drizzle-orm/pg-core";
import { createInsertSchema } from "drizzle-zod";
import { z } from "zod";

export const users = pgTable("users", {
  id: varchar("id").primaryKey(),
  username: text("username").notNull().unique(),
  password: text("password").notNull(),
});

export const vaultLogs = pgTable("vault_logs", {
  id: varchar("id").primaryKey(),
  type: text("type").notNull(), // 'delete' | 'edit'
  authorName: text("author_name").notNull(),
  authorId: text("author_id").notNull(),
  channelName: text("channel_name").notNull(),
  channelId: text("channel_id").notNull(),
  originalContent: text("original_content").notNull(),
  revisedContent: text("revised_content"),
  guildName: text("guild_name").notNull(),
  timestamp: timestamp("timestamp").notNull().defaultNow(),
});

export const botSettings = pgTable("bot_settings", {
  id: varchar("id").primaryKey(),
  guildId: text("guild_id").notNull().unique(),
  guildName: text("guild_name").notNull(),
  vaultChannelId: text("vault_channel_id").notNull(),
  vaultChannelName: text("vault_channel_name").notNull(),
  ownerId: text("owner_id").notNull(),
  botPrefix: text("bot_prefix").notNull().default("e!"),
  isActive: boolean("is_active").notNull().default(true),
  loggingEnabled: boolean("logging_enabled").notNull().default(true),
});

export const commandLogs = pgTable("command_logs", {
  id: varchar("id").primaryKey(),
  commandName: text("command_name").notNull(),
  executedBy: text("executed_by").notNull(),
  executedById: text("executed_by_id").notNull(),
  channelName: text("channel_name").notNull(),
  guildName: text("guild_name").notNull(),
  details: text("details"),
  success: boolean("success").notNull().default(true),
  timestamp: timestamp("timestamp").notNull().defaultNow(),
});

export const insertVaultLogSchema = createInsertSchema(vaultLogs).omit({ timestamp: true });
export const insertBotSettingsSchema = createInsertSchema(botSettings);
export const insertCommandLogSchema = createInsertSchema(commandLogs).omit({ timestamp: true });

const insertUserSchema = z.object({ username: z.string(), password: z.string() });
export type InsertUser = z.infer<typeof insertUserSchema>;
export type User = typeof users.$inferSelect;
export type VaultLog = typeof vaultLogs.$inferSelect;
export type InsertVaultLog = z.infer<typeof insertVaultLogSchema>;
export type BotSettings = typeof botSettings.$inferSelect;
export type InsertBotSettings = z.infer<typeof insertBotSettingsSchema>;
export type CommandLog = typeof commandLogs.$inferSelect;
export type InsertCommandLog = z.infer<typeof insertCommandLogSchema>;
