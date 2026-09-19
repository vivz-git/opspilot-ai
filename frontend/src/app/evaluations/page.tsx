import { FlaskConical } from "lucide-react";

import { ComingSoon } from "@/components/coming-soon";

export default function EvaluationsPage() {
  return (
    <ComingSoon
      icon={FlaskConical}
      title="Evaluations"
      description="Suite runs, metrics and per-case pass/fail with the failing assertion."
      note="The backend evaluation endpoints (API-005) aren't implemented yet, so this surface renders no live data. It is reserved here so the operator console's navigation matches the product surface from day one."
    />
  );
}
