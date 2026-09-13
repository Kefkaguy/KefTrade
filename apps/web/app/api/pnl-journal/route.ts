import { NextRequest, NextResponse } from 'next/server';

export const dynamic = 'force-dynamic';
export async function GET(request: NextRequest) {
  const month = request.nextUrl.searchParams.get('month') ?? '';
  if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(month))
    return NextResponse.json({ error: 'Invalid month' }, { status: 400 });
  try {
    const base = process.env.API_INTERNAL_URL ?? process.env.NEXT_PUBLIC_API_URL ?? 'http://127.0.0.1:8000';
    const response = await fetch(`${base}/broker/pnl-journal?month=${month}`, {
      cache: 'no-store', signal: AbortSignal.timeout(15000),
    });
    if (!response.ok) throw new Error('Backend unavailable');
    return NextResponse.json(await response.json(), { headers: { 'Cache-Control': 'no-store' } });
  } catch {
    return NextResponse.json({ error: 'Broker journal unavailable. Check the API and database connection.' }, { status: 503 });
  }
}
