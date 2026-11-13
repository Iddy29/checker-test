import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/lib/auth";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { ExternalLink, Users, ShieldAlert, RefreshCw, AlertCircle } from "lucide-react";

interface MembershipData {
  member: boolean;
  status: string;
  groupLink?: string;
  channelLink?: string;
}

function getStatusInfo(status: string) {
  switch (status) {
    case "not_in_channel":
      return { title: "Join Our Channel", message: "You need to join our Telegram Channel to use this checker for free.", missingChannel: true };
    default:
      return { title: "Join Our Channel", message: "You need to join our Telegram Channel to use this checker for free.", missingChannel: true };
  }
}

export function MembershipGate({ children }: { children: React.ReactNode }) {
  const { isAuthenticated } = useAuth();
  const queryClient = useQueryClient();
  const [refreshing, setRefreshing] = useState(false);
  const [refreshStatus, setRefreshStatus] = useState<string | null>(null);

  const { data, isLoading } = useQuery<MembershipData>({
    queryKey: ["/api/user/membership"],
    enabled: isAuthenticated,
    staleTime: 60000,
    refetchInterval: 120000,
  });

  if (!isAuthenticated || isLoading || !data || data.member) {
    return <>{children}</>;
  }

  const channelLink = data.channelLink || "";
  const currentStatus = refreshStatus || data.status || "";
  const statusInfo = getStatusInfo(currentStatus);

  const handleRefresh = async () => {
    setRefreshing(true);
    setRefreshStatus(null);
    try {
      const res = await fetch("/api/user/membership?refresh=true", { credentials: "include" });
      const freshData = await res.json();
      queryClient.setQueryData(["/api/user/membership"], freshData);
      if (freshData.member) {
        window.location.reload();
      } else {
        setRefreshStatus(freshData.status || "not_in_channel");
      }
    } catch {
      setRefreshStatus(null);
    }
    setRefreshing(false);
  };

  return (
    <>
      {children}
      <div className="fixed inset-0 z-[100] bg-black/70 backdrop-blur-sm flex items-center justify-center p-4" data-testid="membership-overlay">
        <Card className="w-full max-w-md lg:max-w-lg border-yellow-500/40 bg-background shadow-2xl" data-testid="card-membership">
          <CardHeader className="text-center pb-2 lg:pb-4">
            <div className="flex justify-center mb-3 lg:mb-5">
              <div className="p-3 lg:p-5 rounded-full bg-yellow-500/10 border border-yellow-500/30">
                <ShieldAlert className="w-8 h-8 lg:w-12 lg:h-12 text-yellow-400" />
              </div>
            </div>
            <CardTitle className="text-xl lg:text-2xl">{statusInfo.title}</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col items-center gap-4 lg:gap-6 lg:px-8">
            <p className="text-sm lg:text-base text-muted-foreground text-center">
              {statusInfo.message}
            </p>

            <div className="w-full flex flex-col gap-2 lg:gap-3">
              {channelLink && (
                <Button
                  className="w-full font-semibold lg:h-12 lg:text-base bg-blue-500 hover:bg-blue-600 text-white"
                  onClick={() => window.open(channelLink, "_blank")}
                  data-testid="button-join-channel"
                >
                  <Users className="w-4 h-4 lg:w-5 lg:h-5 mr-2" />
                  Join Telegram Channel
                  <ExternalLink className="w-3.5 h-3.5 lg:w-4 lg:h-4 ml-2" />
                </Button>
              )}
              {!channelLink && (
                <p className="text-xs lg:text-sm text-yellow-400 text-center">
                  Contact the admin to get the channel invite link.
                </p>
              )}
            </div>

            {refreshStatus && (
              <div className="w-full flex items-start gap-2 p-3 rounded-lg bg-yellow-500/10 border border-yellow-500/30">
                <AlertCircle className="w-4 h-4 text-yellow-400 mt-0.5 shrink-0" />
                <p className="text-xs lg:text-sm text-yellow-300">
                  {currentStatus === "not_in_channel"
                    ? "You're not in the Channel yet. Join using the button above and try again."
                    : "Still not detected. Make sure you joined using the button above, wait a moment, then try again."}
                </p>
              </div>
            )}

            <p className="text-xs lg:text-sm text-muted-foreground text-center">
              After joining, click below to continue
            </p>

            <Button
              variant="outline"
              size="sm"
              className="lg:h-10 lg:text-sm lg:px-6"
              onClick={handleRefresh}
              disabled={refreshing}
              data-testid="button-refresh-membership"
            >
              {refreshing ? (
                <><RefreshCw className="w-4 h-4 mr-2 animate-spin" /> Checking...</>
              ) : (
                "I've Joined — Refresh"
              )}
            </Button>
          </CardContent>
        </Card>
      </div>
    </>
  );
}
