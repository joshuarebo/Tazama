"use client";

import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { type ReactNode, useState } from "react";

import { Shell } from "@/components/Shell";
import { getMeta } from "@/lib/api";

export function Providers({ children }: { children: ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            // The console shows operational data: a stale incident list is misleading, but
            // refetching on every focus change is noisy on a wall display. Thirty seconds
            // is the compromise, and the SSE stream invalidates sooner when something
            // actually changes.
            staleTime: 30_000,
            retry: (count, error) => {
              const status = (error as { status?: number }).status ?? 0;
              // Never retry an authorisation failure: it will fail again.
              if (status === 401 || status === 403 || status === 422) return false;
              return count < 2;
            },
            refetchOnWindowFocus: false,
          },
        },
      }),
  );

  return (
    <QueryClientProvider client={client}>
      <WithMeta>{children}</WithMeta>
    </QueryClientProvider>
  );
}

function WithMeta({ children }: { children: ReactNode }) {
  // `/meta` needs no token, so it can be fetched before sign-in to learn the mode,
  // the display timezone and which capabilities each role has.
  const { data } = useQuery({ queryKey: ["meta"], queryFn: getMeta, staleTime: 5 * 60_000 });
  return <Shell meta={data ?? null}>{children}</Shell>;
}
