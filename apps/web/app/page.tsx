import { HomeWorkspace } from "@/components/HomeWorkspace";
import { getMissionControl } from "@/lib/api";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";
export const revalidate = 0;

export default async function HomePage() {
  try {
    const snapshot = await getMissionControl();
    return <HomeWorkspace snapshot={snapshot} />;
  } catch (error) {
    const message = error instanceof Error ? error.message : "Research services are unavailable.";
    return <HomeWorkspace snapshot={null} error={message} />;
  }
}
