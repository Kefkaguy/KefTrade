import { RiskForm } from "@/components/RiskForm";
import { Card, PageTitle } from "@/components/ResearchUI";

export default function SettingsPage() {
  return (
    <div className="pageStack">
      <PageTitle title="Settings" description="Research guardrails, risk configuration, theme controls, and platform status." />
      <Card title="Risk settings" eyebrow="Guardrails">
        <RiskForm />
      </Card>
    </div>
  );
}
