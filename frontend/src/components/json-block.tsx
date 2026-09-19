/**
 * Renders an already-redacted server payload verbatim as formatted JSON.
 * The backend owns redaction (architecture.md §14.5) — this never filters
 * or transforms values, it only formats what it's given.
 */
export function JsonBlock({ label, value }: { label: string; value: unknown }) {
  if (value == null) return null;
  return (
    <div className="flex flex-col gap-1">
      <span className="text-xs font-medium text-muted-foreground">{label}</span>
      <pre className="max-h-48 overflow-auto rounded-md border border-border/60 bg-background p-2 font-mono text-[11px] leading-relaxed">
        {JSON.stringify(value, null, 2)}
      </pre>
    </div>
  );
}
