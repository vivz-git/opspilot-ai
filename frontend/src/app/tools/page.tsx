import { Wrench } from "lucide-react";

import { ComingSoon } from "@/components/coming-soon";

export default function ToolsPage() {
  return (
    <ComingSoon
      icon={Wrench}
      title="Tools"
      description="The tool catalog rendered from the contract registry the agent is bound to."
      note="The backend tools catalog endpoint (API-006, GET /tools) isn't implemented yet, so this surface renders no live data. It is reserved here so the operator console's navigation matches the product surface from day one."
    />
  );
}
