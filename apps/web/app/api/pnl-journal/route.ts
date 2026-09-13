import { NextRequest, NextResponse } from 'next/server';

export const dynamic = 'force-dynamic';
export async function GET(request: NextRequest) {
  const month = request.nextUrl.searchParams.get('month') ?? '';
  if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(month))
    return NextResponse.json({ error: 'Invalid month' }, { status: 400 });
  const base = process.env.API_INTERNAL_URL
    ?? process.env.NEXT_PUBLIC_API_URL
    ?? (process.env.NODE_ENV === 'production' ? 'https://keftrade.duckdns.org' : 'http://127.0.0.1:8002');
  try {
    const response = await fetch(`${base}/broker/pnl-journal?month=${month}`, {
      cache: 'no-store', signal: AbortSignal.timeout(15000),
    });
    if (!response.ok) {
      const detail = await response.text().catch(() => '');
      return NextResponse.json({
        error: 'Broker journal unavailable. The API returned an error.',
        status: response.status,
        detail: process.env.NODE_ENV === 'production' ? undefined : detail.slice(0, 500),
        upstream: process.env.NODE_ENV === 'production' ? undefined : base,
      }, { status: 503 });
    }
    return NextResponse.json(await response.json(), { headers: { 'Cache-Control': 'no-store' } });
  } catch (error) {
    return NextResponse.json({
      error: 'Broker journal unavailable. Check the API and database connection.',
      detail: process.env.NODE_ENV === 'production' && error instanceof Error ? undefined : error instanceof Error ? error.message : String(error),
      upstream: process.env.NODE_ENV === 'production' ? undefined : base,
    }, { status: 503 });
  }
}
