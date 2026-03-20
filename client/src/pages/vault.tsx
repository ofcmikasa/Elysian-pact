import { useQuery, useMutation } from "@tanstack/react-query";
import { queryClient, apiRequest } from "@/lib/queryClient";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Archive, Trash2, PenLine, Filter, X, RefreshCw, AlertTriangle } from "lucide-react";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import type { VaultLog } from "@shared/schema";
import { formatDistanceToNow, format } from "date-fns";
import { useState } from "react";
import { useToast } from "@/hooks/use-toast";

function LogTypeBadge({ type }: { type: string }) {
  if (type === "delete") {
    return (
      <Badge variant="destructive" className="text-[10px] gap-1">
        <Trash2 className="w-2.5 h-2.5" />
        Deleted
      </Badge>
    );
  }
  return (
    <Badge variant="secondary" className="text-[10px] gap-1 bg-primary/10 text-primary border-primary/20">
      <PenLine className="w-2.5 h-2.5" />
      Edited
    </Badge>
  );
}

function VaultLogCard({ log, onDelete }: { log: VaultLog; onDelete: (id: string) => void }) {
  return (
    <Card data-testid={`vault-log-${log.id}`} className="transition-colors">
      <CardContent className="p-4">
        <div className="flex items-start gap-3">
          <div className={`w-8 h-8 rounded-md flex items-center justify-center flex-shrink-0 mt-0.5 ${log.type === "delete" ? "bg-destructive/10" : "bg-primary/10"}`}>
            {log.type === "delete" ? (
              <Trash2 className="w-4 h-4 text-destructive" />
            ) : (
              <PenLine className="w-4 h-4 text-primary" />
            )}
          </div>

          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap mb-2">
              <LogTypeBadge type={log.type} />
              <span className="text-sm font-medium text-foreground">{log.authorName}</span>
              <span className="text-xs text-muted-foreground">#{log.channelName}</span>
              <span className="text-xs text-muted-foreground ml-auto flex-shrink-0">
                {formatDistanceToNow(new Date(log.timestamp), { addSuffix: true })}
              </span>
            </div>

            <div className="space-y-2">
              <div className="rounded-md bg-muted/50 border border-border p-2.5">
                <p className="text-[11px] text-muted-foreground font-medium mb-1 uppercase tracking-wide">
                  {log.type === "edit" ? "Before" : "Content"}
                </p>
                <p className="text-sm text-foreground leading-relaxed break-words">{log.originalContent}</p>
              </div>

              {log.type === "edit" && log.revisedContent && (
                <div className="rounded-md bg-primary/5 border border-primary/15 p-2.5">
                  <p className="text-[11px] text-primary/70 font-medium mb-1 uppercase tracking-wide">After</p>
                  <p className="text-sm text-foreground leading-relaxed break-words">{log.revisedContent}</p>
                </div>
              )}
            </div>

            <div className="flex items-center justify-between mt-3">
              <div className="flex items-center gap-3">
                <span className="text-[11px] text-muted-foreground">ID: {log.authorId}</span>
                <span className="text-[11px] text-muted-foreground">{format(new Date(log.timestamp), "MMM d, HH:mm")}</span>
              </div>
              <Button
                size="icon"
                variant="ghost"
                className="h-7 w-7 text-muted-foreground"
                onClick={() => onDelete(log.id)}
                data-testid={`button-delete-log-${log.id}`}
              >
                <X className="w-3.5 h-3.5" />
              </Button>
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

export default function Vault() {
  const [filter, setFilter] = useState("all");
  const { toast } = useToast();

  const { data: logs, isLoading, refetch } = useQuery<VaultLog[]>({
    queryKey: ["/api/vault-logs"],
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => apiRequest("DELETE", `/api/vault-logs/${id}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["/api/vault-logs"] });
      queryClient.invalidateQueries({ queryKey: ["/api/stats"] });
      toast({ title: "Log entry removed from the vault." });
    },
  });

  const clearMutation = useMutation({
    mutationFn: () => apiRequest("DELETE", "/api/vault-logs"),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["/api/vault-logs"] });
      queryClient.invalidateQueries({ queryKey: ["/api/stats"] });
      toast({ title: "The vault has been cleared." });
    },
  });

  const filtered = (logs ?? []).filter(l => filter === "all" || l.type === filter);

  return (
    <div className="flex flex-col gap-5 p-6 max-w-3xl mx-auto w-full">
      <div>
        <div className="flex items-center gap-2.5 mb-1">
          <Archive className="w-5 h-5 text-primary" />
          <h1 className="text-xl font-bold">The Vault</h1>
        </div>
        <p className="text-sm text-muted-foreground">All deleted and edited messages captured by Elysian.</p>
      </div>

      <div className="flex items-center gap-3 flex-wrap">
        <div className="flex items-center gap-2 flex-shrink-0">
          <Filter className="w-3.5 h-3.5 text-muted-foreground" />
          <Select value={filter} onValueChange={setFilter}>
            <SelectTrigger className="w-36 h-9" data-testid="select-filter">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All entries</SelectItem>
              <SelectItem value="delete">Deleted</SelectItem>
              <SelectItem value="edit">Edited</SelectItem>
            </SelectContent>
          </Select>
        </div>

        <Badge variant="secondary" className="text-xs">
          {filtered.length} {filtered.length === 1 ? "entry" : "entries"}
        </Badge>

        <div className="ml-auto flex items-center gap-2">
          <Button
            size="icon"
            variant="ghost"
            onClick={() => refetch()}
            data-testid="button-refresh"
          >
            <RefreshCw className="w-4 h-4" />
          </Button>

          <AlertDialog>
            <AlertDialogTrigger asChild>
              <Button
                variant="outline"
                size="sm"
                className="text-destructive border-destructive/30"
                disabled={filtered.length === 0 || clearMutation.isPending}
                data-testid="button-clear-vault"
              >
                <Trash2 className="w-3.5 h-3.5 mr-1.5" />
                Clear All
              </Button>
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle className="flex items-center gap-2">
                  <AlertTriangle className="w-4 h-4 text-destructive" />
                  Clear the Vault?
                </AlertDialogTitle>
                <AlertDialogDescription>
                  This will permanently remove all {filtered.length} vault entries. This action cannot be undone.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>Cancel</AlertDialogCancel>
                <AlertDialogAction
                  className="bg-destructive text-destructive-foreground"
                  onClick={() => clearMutation.mutate()}
                >
                  Clear Vault
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        </div>
      </div>

      {isLoading ? (
        <div className="flex flex-col gap-3">
          {[...Array(4)].map((_, i) => <Skeleton key={i} className="h-36 w-full rounded-lg" />)}
        </div>
      ) : filtered.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-16 text-center gap-3">
          <div className="w-14 h-14 rounded-full bg-muted flex items-center justify-center">
            <Archive className="w-7 h-7 text-muted-foreground/50" />
          </div>
          <div>
            <p className="font-medium text-foreground">The vault is empty</p>
            <p className="text-sm text-muted-foreground mt-0.5">No messages have been captured yet.</p>
          </div>
        </div>
      ) : (
        <div className="flex flex-col gap-3">
          {filtered.map((log) => (
            <VaultLogCard
              key={log.id}
              log={log}
              onDelete={(id) => deleteMutation.mutate(id)}
            />
          ))}
        </div>
      )}
    </div>
  );
}
