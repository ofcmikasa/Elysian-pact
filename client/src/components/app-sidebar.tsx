import { Link, useLocation } from "wouter";
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarFooter,
} from "@/components/ui/sidebar";
import { LayoutDashboard, Archive, Settings, Terminal, BookOpen, Circle } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { useQuery } from "@tanstack/react-query";
import type { VaultLog } from "@shared/schema";

const navItems = [
  { title: "Overview", url: "/", icon: LayoutDashboard },
  { title: "The Vault", url: "/vault", icon: Archive },
  { title: "Commands", url: "/commands", icon: Terminal },
  { title: "Settings", url: "/settings", icon: Settings },
];

export function AppSidebar() {
  const [location] = useLocation();

  const { data: logs } = useQuery<VaultLog[]>({
    queryKey: ["/api/vault-logs"],
  });

  const recentCount = logs?.filter(l => {
    const diff = Date.now() - new Date(l.timestamp).getTime();
    return diff < 1000 * 60 * 60;
  }).length ?? 0;

  return (
    <Sidebar>
      <SidebarHeader className="px-4 py-5">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-md bg-primary flex items-center justify-center flex-shrink-0 elysian-glow-sm">
            <BookOpen className="w-4 h-4 text-primary-foreground" />
          </div>
          <div>
            <p className="font-semibold text-sm text-sidebar-foreground leading-tight">Elysian</p>
            <p className="text-xs text-muted-foreground leading-tight">Library Guardian</p>
          </div>
        </div>
      </SidebarHeader>

      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupLabel>Navigation</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {navItems.map((item) => {
                const isActive = location === item.url;
                return (
                  <SidebarMenuItem key={item.title}>
                    <SidebarMenuButton asChild data-active={isActive}>
                      <Link href={item.url} data-testid={`nav-${item.title.toLowerCase().replace(/\s/g, "-")}`}>
                        <item.icon className="w-4 h-4" />
                        <span>{item.title}</span>
                        {item.title === "The Vault" && recentCount > 0 && (
                          <Badge
                            className="ml-auto text-[10px] px-1.5 py-0"
                            variant="default"
                          >
                            {recentCount}
                          </Badge>
                        )}
                      </Link>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                );
              })}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>

      <SidebarFooter className="px-4 py-4">
        <div className="flex items-center gap-2.5">
          <div className="relative flex-shrink-0">
            <div className="w-2 h-2 rounded-full bg-status-online" />
            <div className="absolute inset-0 rounded-full bg-status-online animate-ping opacity-60" />
          </div>
          <div>
            <p className="text-xs font-medium text-sidebar-foreground">Bot Online</p>
            <p className="text-[11px] text-muted-foreground">Guardian Active</p>
          </div>
        </div>
      </SidebarFooter>
    </Sidebar>
  );
}
