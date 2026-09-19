"use client";

import * as React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ApiError } from "@/lib/api/client";

export function Providers({ children }: { children: React.ReactNode }) {
  const [queryClient] = React.useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 10_000,
            // A 404 is a permanent condition — retrying it wastes time and
            // delays the not-found state; anything else (a network blip, a
            // 5xx) gets one retry.
            retry: (failureCount, error) =>
              !(error instanceof ApiError && error.status === 404) && failureCount < 1,
          },
        },
      })
  );

  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}
