import { Switch, Route, useLocation, Link } from "wouter";
import { SidebarProvider, SidebarTrigger } from "@/components/ui/sidebar";
import { AdminSidebar } from "@/components/admin-sidebar";
import { ThemeToggle } from "@/components/theme-toggle";
import Dashboard from "@/pages/dashboard";
import UsersPage from "@/pages/users";
import BotControl from "@/pages/bot-control";
import GatewaysPage from "@/pages/gateways";
import SettingsPage from "@/pages/settings";
import FakeLogsPage from "@/pages/fake-logs";
import NotFound from "@/pages/not-found";

export default function AdminLayout() {
  const style = {
    "--sidebar-width": "16rem",
    "--sidebar-width-icon": "3rem",
  };

  return (
    <SidebarProvider style={style as React.CSSProperties}>
      <div className="flex h-screen w-full">
        <AdminSidebar />
        <div className="flex flex-col flex-1 min-w-0">
          <header className="flex items-center justify-between gap-2 p-2 border-b sticky top-0 z-50 bg-background">
            <SidebarTrigger data-testid="button-sidebar-toggle" />
            <ThemeToggle />
          </header>
          <main className="flex-1 overflow-auto">
            <Switch>
              <Route path="/admin" component={Dashboard} />
              <Route path="/admin/users" component={UsersPage} />
              <Route path="/admin/bot" component={BotControl} />
              <Route path="/admin/gateways" component={GatewaysPage} />
              <Route path="/admin/settings" component={SettingsPage} />
              <Route path="/admin/fake-logs" component={FakeLogsPage} />
              <Route component={NotFound} />
            </Switch>
          </main>
        </div>
      </div>
    </SidebarProvider>
  );
}
