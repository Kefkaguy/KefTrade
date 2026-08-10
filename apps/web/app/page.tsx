import { PaperLabDashboard } from "@/components/PaperLabDashboard";
import { getIntradayPaperLabMonitor } from "@/lib/api";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";
export const revalidate = 0;

export default async function HomePage() {
  try {
    const snapshot = await getIntradayPaperLabMonitor(1);
    return <PaperLabDashboard initial={snapshot} experimentId={1} />;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return <PaperLabDashboard initial={null} experimentId={1} initialError={message} />;
  }
}
