import { useQuery } from "@tanstack/react-query";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Terminal, CheckCircle2, XCircle, Clock, Shield, Trash2, Lock } from "lucide-react";
import type { CommandLog } from "@shared/schema";
import { formatDistanceToNow, format } from "date-fns";

const commandDocs = [
  {
    name: "purge",
    description: "Removes a specified number of messages from the current channel.",
    usage: "/purge <amount>",
    icon: Trash2,
    access: "Owner Only",
  },
  {
    name: "lock",
    description: "Closes the current channel, preventing members from sending messages.",
    usage: "/lock",
    icon: Lock,
    access: "Owner Only",
  },
];

export default function Commands() {
  const { data: logs, isLoading } = useQuery<CommandLog[]>({
    queryKey: ["/api/command-logs"],
  });

  return (
    <div className="flex flex-col gap-6 p-6 max-w-3xl mx-auto w-full">
      <div>
        <div className="flex items-center gap-2.5 mb-1">
          <Terminal className="w-5 h-5 text-primary" />
          <h1 className="text-xl font-bold">Commands</h1>
        </div>
        <p className="text-sm text-muted-foreground">Available slash commands and execution history.</p>
      </div>

      <div>
        <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-3">Available Commands</p>
        <div className="flex flex-col gap-3">
          {commandDocs.map((cmd) => (
            <Card key={cmd.name} data-testid={`command-doc-${cmd.name}`}>
              <CardContent className="p-4">
                <div className="flex items-start gap-3">
                  <div className="w-9 h-9 rounded-md bg-primary/10 flex items-center justify-center flex-shrink-0">
                    <cmd.icon className="w-4 h-4 text-primary" />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap mb-1">
                      <span className="font-mono text-sm font-semibold text-foreground">/{cmd.name}</span>
                      <Badge variant="secondary" className="text-[10px] gap-1">
                        <Shield className="w-2.5 h-2.5" />
                        {cmd.access}
                      </Badge>
                    </div>
                    <p className="text-sm text-muted-foreground mb-2">{cmd.description}</p>
                    <div className="rounded bg-muted/50 border border-border px-2.5 py-1.5 inline-block">
                      <code className="text-xs font-mono text-foreground">{cmd.usage}</code>
                    </div>
                  </div>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      </div>

      <div>
        <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-3">Execution History</p>
        {isLoading ? (
          <div className="flex flex-col gap-3">
            {[...Array(3)].map((_, i) => <Skeleton key={i} className="h-20 w-full rounded-lg" />)}
          </div>
        ) : !logs || logs.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-12 text-center gap-3">
            <div className="w-12 h-12 rounded-full bg-muted flex items-center justify-center">
              <Terminal className="w-6 h-6 text-muted-foreground/50" />
            </div>
            <div>
              <p className="font-medium text-foreground">No commands used yet</p>
              <p className="text-sm text-muted-foreground mt-0.5">Command executions will appear here.</p>
            </div>
          </div>
        ) : (
          <div className="flex flex-col gap-2.5">
            {logs.map((cmd) => (
              <div
                key={cmd.id}
                data-testid={`command-log-${cmd.id}`}
                className="flex items-center gap-3 p-3.5 rounded-md border border-border bg-card"
              >
                <div className={`w-7 h-7 rounded flex items-center justify-center flex-shrink-0 ${cmd.success ? "bg-status-online/10" : "bg-destructive/10"}`}>
                  {cmd.success ? (
                    <CheckCircle2 className="w-3.5 h-3.5 text-status-online" />
                  ) : (
                    <XCircle className="w-3.5 h-3.5 text-destructive" />
                  )}
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-0.5">
                    <span className="font-mono text-sm font-medium text-foreground">/{cmd.commandName}</span>
                    <span className="text-xs text-muted-foreground">by {cmd.executedBy}</span>
                    <span className="text-xs text-muted-foreground">in #{cmd.channelName}</span>
                  </div>
                  {cmd.details && (
                    <p className="text-xs text-muted-foreground">{cmd.details}</p>
                  )}
                </div>
                <div className="flex items-center gap-1 flex-shrink-0">
                  <Clock className="w-3 h-3 text-muted-foreground/60" />
                  <span className="text-[11px] text-muted-foreground">
                    {formatDistanceToNow(new Date(cmd.timestamp), { addSuffix: true })}
                  </span>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
