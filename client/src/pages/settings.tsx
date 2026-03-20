import { useQuery, useMutation } from "@tanstack/react-query";
import { queryClient, apiRequest } from "@/lib/queryClient";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Skeleton } from "@/components/ui/skeleton";
import { Settings, Save, Server, Hash, User, Activity } from "lucide-react";
import type { BotSettings } from "@shared/schema";
import { useToast } from "@/hooks/use-toast";
import { useForm } from "react-hook-form";
import { useEffect } from "react";

export default function SettingsPage() {
  const { toast } = useToast();

  const { data: settingsList, isLoading } = useQuery<BotSettings[]>({
    queryKey: ["/api/settings"],
  });

  const settings = settingsList?.[0];

  const form = useForm<Partial<BotSettings>>({
    defaultValues: {
      guildName: "",
      vaultChannelName: "",
      botPrefix: "e!",
      isActive: true,
      loggingEnabled: true,
    },
  });

  useEffect(() => {
    if (settings) {
      form.reset({
        guildName: settings.guildName,
        vaultChannelName: settings.vaultChannelName,
        botPrefix: settings.botPrefix,
        isActive: settings.isActive,
        loggingEnabled: settings.loggingEnabled,
      });
    }
  }, [settings]);

  const saveMutation = useMutation({
    mutationFn: (data: Partial<BotSettings>) =>
      apiRequest("PUT", "/api/settings", {
        ...settings,
        ...data,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["/api/settings"] });
      toast({ title: "Settings saved successfully." });
    },
    onError: () => {
      toast({ title: "Failed to save settings.", variant: "destructive" });
    },
  });

  const onSubmit = form.handleSubmit((data) => {
    saveMutation.mutate(data);
  });

  return (
    <div className="flex flex-col gap-6 p-6 max-w-2xl mx-auto w-full">
      <div>
        <div className="flex items-center gap-2.5 mb-1">
          <Settings className="w-5 h-5 text-primary" />
          <h1 className="text-xl font-bold">Settings</h1>
        </div>
        <p className="text-sm text-muted-foreground">Configure your Elysian bot preferences.</p>
      </div>

      {isLoading ? (
        <div className="flex flex-col gap-4">
          <Skeleton className="h-48 w-full rounded-lg" />
          <Skeleton className="h-36 w-full rounded-lg" />
        </div>
      ) : !settings ? (
        <Card>
          <CardContent className="py-12 text-center">
            <p className="text-muted-foreground">No bot configuration found.</p>
          </CardContent>
        </Card>
      ) : (
        <form onSubmit={onSubmit} className="flex flex-col gap-4">
          <Card>
            <CardHeader className="pb-4">
              <CardTitle className="text-sm font-semibold flex items-center gap-2">
                <Server className="w-4 h-4 text-primary" />
                Server Information
              </CardTitle>
              <CardDescription className="text-xs">
                Details about the connected Discord server.
              </CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-4">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="guildName" className="text-xs font-medium">Server Name</Label>
                <Input
                  id="guildName"
                  {...form.register("guildName")}
                  data-testid="input-guild-name"
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label className="text-xs font-medium">Server ID</Label>
                <div className="flex items-center gap-2">
                  <Input value={settings.guildId} readOnly className="font-mono text-xs text-muted-foreground" />
                  <Badge variant="secondary" className="flex-shrink-0 text-xs">Read-only</Badge>
                </div>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-4">
              <CardTitle className="text-sm font-semibold flex items-center gap-2">
                <Hash className="w-4 h-4 text-primary" />
                Vault Configuration
              </CardTitle>
              <CardDescription className="text-xs">
                Configure where deleted and edited messages are logged.
              </CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-4">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="vaultChannelName" className="text-xs font-medium">Vault Channel Name</Label>
                <Input
                  id="vaultChannelName"
                  {...form.register("vaultChannelName")}
                  data-testid="input-vault-channel"
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label className="text-xs font-medium">Vault Channel ID</Label>
                <Input value={settings.vaultChannelId} readOnly className="font-mono text-xs text-muted-foreground" />
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-4">
              <CardTitle className="text-sm font-semibold flex items-center gap-2">
                <User className="w-4 h-4 text-primary" />
                Admin Configuration
              </CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-4">
              <div className="flex flex-col gap-1.5">
                <Label className="text-xs font-medium">Owner ID</Label>
                <Input value={settings.ownerId} readOnly className="font-mono text-xs text-muted-foreground" />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="botPrefix" className="text-xs font-medium">Bot Prefix</Label>
                <Input
                  id="botPrefix"
                  {...form.register("botPrefix")}
                  className="font-mono max-w-24"
                  data-testid="input-bot-prefix"
                />
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-4">
              <CardTitle className="text-sm font-semibold flex items-center gap-2">
                <Activity className="w-4 h-4 text-primary" />
                Bot Behavior
              </CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-4">
              <div className="flex items-center justify-between gap-4">
                <div>
                  <p className="text-sm font-medium">Bot Active</p>
                  <p className="text-xs text-muted-foreground">Enable or disable the bot entirely.</p>
                </div>
                <Switch
                  checked={form.watch("isActive") ?? true}
                  onCheckedChange={(val) => form.setValue("isActive", val)}
                  data-testid="switch-bot-active"
                />
              </div>
              <div className="flex items-center justify-between gap-4">
                <div>
                  <p className="text-sm font-medium">Message Logging</p>
                  <p className="text-xs text-muted-foreground">Log deleted and edited messages to the vault.</p>
                </div>
                <Switch
                  checked={form.watch("loggingEnabled") ?? true}
                  onCheckedChange={(val) => form.setValue("loggingEnabled", val)}
                  data-testid="switch-logging-enabled"
                />
              </div>
            </CardContent>
          </Card>

          <Button
            type="submit"
            disabled={saveMutation.isPending}
            className="self-end"
            data-testid="button-save-settings"
          >
            <Save className="w-4 h-4 mr-1.5" />
            {saveMutation.isPending ? "Saving..." : "Save Settings"}
          </Button>
        </form>
      )}
    </div>
  );
}
