import { PnlJournal } from "@/components/PnlJournal";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";
export const revalidate = 0;

export default function HomePage() {
  return <PnlJournal />;
}
