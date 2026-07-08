"use client";

import { EmptyState } from "@/components/ResearchUI";

export default function Error({ reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <EmptyState
      title="Research view failed to load."
      body="The API may be unavailable or a research response may be incomplete. Retry after confirming the backend is running."
      action={<button className="button" type="button" onClick={reset}>Retry</button>}
    />
  );
}
