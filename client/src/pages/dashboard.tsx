import { useQuery } from "@tanstack/react-query";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Archive, Trash2, PenLine, Terminal, BookOpen, Server, Clock } from "lucide-react";
import type { VaultLog, CommandLog } from "@shared/schema";
import { formatDistanceToNow } from "date-fns";

interface Stats {
  totalLogs: number;
  deletedMessages: number;
  editedMessages: number;
  totalCommands: number;
  activeGuilds: number;
}

function StatCard({
  label,
  value,
  icon: Icon,
  color,
  loading,
}: {
  label: string;
  value: number | undefined;
  icon: React.ElementType;
  color: string;
  loading: boolean;
}) {
  return (
    <Card data-testid={`card-stat-${label.toLowerCase().replace(/\s/g, "-")}`}>
      <CardContent className="p-5">
        <div className="flex items-center justify-between gap-4">
          <div>
            <p className="text-xs text-muted-foreground font-medium mb-1">{label}</p>
            {loading ? (
              <Skeleton className="h-7 w-12" />
            ) : (
              <p className="text-2xl font-bold text-foreground">{value?.toLocaleString()}</p>
            )}
          </div>
          <div className={`w-10 h-10 rounded-md flex items-center justify-center flex-shrink-0 ${color}`}>
            <Icon className="w-5 h-5" />
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

function LogTypeIcon({ type }: { type: string }) {
  if (type === "delete") {
    return (
      <div className="w-7 h-7 rounded flex items-center justify-center bg-destructive/10 flex-shrink-0">
        <Trash2 className="w-3.5 h-3.5 text-destructive" />
      </div>
    );
  }
  return (
    <div className="w-7 h-7 rounded flex items-center justify-center bg-primary/10 flex-shrink-0">
      <PenLine className="w-3.5 h-3.5 text-primary" />
    </div>
  );
}

export default function Dashboard() {
  const { data: stats, isLoading: statsLoading } = useQuery<Stats>({
    queryKey: ["/api/stats"],
  });

  const { data: logs, isLoading: logsLoading } = useQuery<VaultLog[]>({
    queryKey: ["/api/vault-logs"],
  });

  const { data: commands, isLoading: commandsLoading } = useQuery<CommandLog[]>({
    queryKey: ["/api/command-logs"],
  });

  const recentLogs = logs?.slice(0, 5) ?? [];
  const recentCommands = commands?.slice(0, 4) ?? [];

  return (
    <div className="flex flex-col gap-6 p-6 max-w-5xl mx-auto w-full">
      <div>
        <div className="flex items-center gap-2.5 mb-1">
          <BookOpen className="w-5 h-5 text-primary" />
          <h1 className="text-xl font-bold">Overview</h1>
        </div>
        <p className="text-sm text-muted-foreground">Welcome to the Elysian Library guardian dashboard.</p>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatCard label="Vault Entries" value={stats?.totalLogs} icon={Archive} color="bg-primary/10 text-primary" loading={statsLoading} />
        <StatCard label="Deleted" value={stats?.deletedMessages} icon={Trash2} color="bg-destructive/10 text-destructive" loading={statsLoading} />
        <StatCard label="Edited" value={stats?.editedMessages} icon={PenLine} color="bg-accent text-accent-foreground" loading={statsLoading} />
        <StatCard label="Commands" value={stats?.totalCommands} icon={Terminal} color="bg-secondary text-secondary-foreground" loading={statsLoading} />
      </div>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-5">
        <Card className="md:col-span-3">
          <CardHeader className="pb-3">
            <div className="flex items-center justify-between gap-2">
              <CardTitle className="text-sm font-semibold flex items-center gap-2">
                <Archive className="w-4 h-4 text-primary" />
                Recent Vault Activity
              </CardTitle>
              <Badge variant="secondary" className="text-[10px]">
                {logs?.length ?? 0} total
              </Badge>
            </div>
          </CardHeader>
          <CardContent className="px-4 pb-4">
            {logsLoading ? (
              <div className="flex flex-col gap-3">
                {[...Array(4)].map((_, i) => <Skeleton key={i} className="h-12 w-full" />)}
              </div>
            ) : recentLogs.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-8 text-center gap-2">
                <Archive className="w-8 h-8 text-muted-foreground/40" />
                <p className="text-sm text-muted-foreground">No vault entries yet.</p>
              </div>
            ) : (
              <div className="flex flex-col gap-2.5">
                {recentLogs.map((log) => (
                  <div
                    key={log.id}
                    data-testid={`log-item-${log.id}`}
                    className="flex items-start gap-3 p-3 rounded-md bg-muted/40 border border-border"
                  >
                    <LogTypeIcon type={log.type} />
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap mb-0.5">
                        <span className="text-xs font-medium text-foreground">{log.authorName}</span>
                        <span className="text-muted-foreground text-[10px]">in #{log.channelName}</span>
                      </div>
                      <p className="text-xs text-muted-foreground truncate">
                        {log.originalContent}
                      </p>
                    </div>
                    <span className="text-[10px] text-muted-foreground whitespace-nowrap flex-shrink-0">
                      {formatDistanceToNow(new Date(log.timestamp), { addSuffix: true })}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>

        <Card className="md:col-span-2">
          <CardHeader className="pb-3">
            <CardTitle className="text-sm font-semibold flex items-center gap-2">
              <Terminal className="w-4 h-4 text-primary" />
              Command History
            </CardTitle>
          </CardHeader>
          <CardContent className="px-4 pb-4">
            {commandsLoading ? (
              <div className="flex flex-col gap-3">
                {[...Array(3)].map((_, i) => <Skeleton key={i} className="h-14 w-full" />)}
              </div>
            ) : recentCommands.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-8 text-center gap-2">
                <Terminal className="w-8 h-8 text-muted-foreground/40" />
                <p className="text-sm text-muted-foreground">No commands used yet.</p>
              </div>
            ) : (
              <div className="flex flex-col gap-2.5">
                {recentCommands.map((cmd) => (
                  <div
                    key={cmd.id}
                    data-testid={`command-item-${cmd.id}`}
                    className="p-3 rounded-md border border-border bg-muted/30"
                  >
                    <div className="flex items-center justify-between gap-2 mb-0.5">
                      <div className="flex items-center gap-1.5">
                        <span className="font-mono text-xs font-medium text-primary">/{cmd.commandName}</span>
                        <Badge variant={cmd.success ? "secondary" : "destructive"} className="text-[10px] px-1.5 py-0">
                          {cmd.success ? "OK" : "ERR"}
                        </Badge>
                      </div>
                    </div>
                    <p className="text-[11px] text-muted-foreground">{cmd.details}</p>
                    <div className="flex items-center gap-1 mt-1">
                      <Clock className="w-3 h-3 text-muted-foreground/60" />
                      <span className="text-[10px] text-muted-foreground">
                        {formatDistanceToNow(new Date(cmd.timestamp), { addSuffix: true })}
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardContent className="p-5">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-md bg-primary/10 flex items-center justify-center flex-shrink-0">
              <Server className="w-4 h-4 text-primary" />
            </div>
            <div className="flex-1">
              <p className="text-sm font-semibold text-foreground">The Elysian Library</p>
              <p className="text-xs text-muted-foreground">Active guild — vault monitoring enabled</p>
            </div>
            <div className="flex items-center gap-2 flex-shrink-0">
              <div className="w-2 h-2 rounded-full bg-status-online" />
              <span className="text-xs text-muted-foreground">Online</span>
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
