"use client";

import { ChevronLeft, ChevronRight, Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";

export function RunsPagination({
  visibleCount,
  totalEstimate,
  hasPreviousPage,
  hasNextPage,
  isFetching,
  onPrevious,
  onNext,
}: {
  visibleCount: number;
  totalEstimate: number;
  hasPreviousPage: boolean;
  hasNextPage: boolean;
  isFetching: boolean;
  onPrevious: () => void;
  onNext: () => void;
}) {
  return (
    <div className="flex items-center justify-between gap-4 text-xs text-muted-foreground">
      <div className="flex items-center gap-2">
        <span>
          {visibleCount} of ~{totalEstimate}
        </span>
        {isFetching && <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />}
      </div>
      <div className="flex items-center gap-2">
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={!hasPreviousPage || isFetching}
          onClick={onPrevious}
        >
          <ChevronLeft className="h-3.5 w-3.5" aria-hidden />
          Previous
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={!hasNextPage || isFetching}
          onClick={onNext}
        >
          Next
          <ChevronRight className="h-3.5 w-3.5" aria-hidden />
        </Button>
      </div>
    </div>
  );
}
