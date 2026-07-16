import { HomeWorkspace } from "@/components/HomeWorkspace";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";
export const revalidate = 0;

export default async function HomePage() {
  return <HomeWorkspace snapshot={null} />;
}
