import { PaperLabDashboard } from "@/components/PaperLabDashboard";
import { getIntradayPaperLabOverview } from "@/lib/api";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";
export const revalidate = 0;

export default async function HomePage() {
  try {
    const snapshot = await getIntradayPaperLabOverview();
    return <PaperLabDashboard initial={snapshot} />;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return <PaperLabDashboard initial={null} initialError={message} />;
  }
}
