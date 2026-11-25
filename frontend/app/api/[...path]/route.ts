import { NextRequest, NextResponse } from 'next/server';

const base = process.env.API_INTERNAL_URL ?? 'http://localhost:8000';
const uploadLimit = Number(process.env.MAX_UPLOAD_BYTES ?? '10485760') + 65536;
const otherBodyLimit = 1024 * 1024;

async function readLimitedBody(request: NextRequest, limit:number): Promise<Uint8Array> {
  const reader = request.body?.getReader();
  if (!reader) return new Uint8Array();
  const chunks:Uint8Array[] = []; let size=0;
  try {
    while (true) {
      const {done,value} = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > limit) {await reader.cancel(); throw new Error('too large');}
      chunks.push(value);
    }
  } finally {reader.releaseLock();}
  const result = new Uint8Array(size); let offset=0;
  for (const chunk of chunks) {result.set(chunk, offset); offset += chunk.byteLength;}
  return result;
}

async function proxy(request: NextRequest, context: {params: Promise<{path: string[]}>}) {
  const {path} = await context.params;
  const route = path.map(encodeURIComponent).join('/');
  if (request.method !== 'GET') {
    try {
      const origin = new URL(request.headers.get('origin') ?? '');
      if (!['http:', 'https:'].includes(origin.protocol) || origin.host !== request.headers.get('host')) throw new Error('origin mismatch');
    } catch {
      return NextResponse.json({detail: 'Invalid request origin'}, {status: 403});
    }
  }
  const headers = new Headers();
  const contentType = request.headers.get('content-type');
  if (contentType) headers.set('content-type', contentType);
  const token = request.cookies.get('mediagent_access')?.value;
  if (token) headers.set('authorization', `Bearer ${token}`);
  let body: BodyInit | undefined;
  const upload = (route === 'documents' || route === 'admin/knowledge/imports') && request.method === 'POST';
  if (request.method !== 'GET') {
    if (upload) {
      const length=request.headers.get('content-length');
      if (length && (!/^\d+$/.test(length) || Number(length) > uploadLimit)) return NextResponse.json({detail:'Request too large'}, {status:413});
      body = request.body ?? undefined;
    } else if (route === 'auth/refresh' || route === 'auth/logout') {
      body = JSON.stringify({refresh_token: request.cookies.get('mediagent_refresh')?.value ?? ''});
    } else {
      try {body = await readLimitedBody(request, otherBodyLimit) as unknown as BodyInit;}
      catch {return NextResponse.json({detail:'Request too large'}, {status:413});}
    }
    if (route === 'auth/refresh' || route === 'auth/logout') headers.set('content-type', 'application/json');
  }
  let upstream: Response;
  try {
    const options: RequestInit & {duplex?:'half'} = {method: request.method, headers, body, cache: 'no-store'};
    if (upload && body) options.duplex='half';
    upstream = await fetch(`${base}/${route}${request.nextUrl.search}`, options);
  } catch {
    const response = NextResponse.json({detail: 'API unavailable'}, {status: 503});
    if (route === 'auth/logout') {response.cookies.delete('mediagent_access'); response.cookies.delete('mediagent_refresh');}
    return response;
  }
  if ((route === 'auth/login' || route === 'auth/refresh') && upstream.ok) {
    const data = await upstream.json();
    const response = NextResponse.json({user: data.user ?? null}, {status: upstream.status});
    response.headers.set('Cache-Control', 'no-store');
    const requestId = upstream.headers.get('x-request-id');
    if (requestId) response.headers.set('x-request-id', requestId);
    const common = {httpOnly: true, sameSite: 'strict' as const, secure: process.env.NODE_ENV === 'production' && process.env.COOKIE_SECURE !== 'false', path: '/'};
    response.cookies.set('mediagent_access', data.access_token, {...common, maxAge: 900});
    response.cookies.set('mediagent_refresh', data.refresh_token, {...common, maxAge: 604800});
    return response;
  }
  const responseHeaders = new Headers();
  for (const key of ['content-type', 'content-disposition', 'x-content-type-options', 'content-security-policy', 'cache-control', 'x-request-id', 'x-next-cursor']) {
    const value = upstream.headers.get(key);
    if (value) responseHeaders.set(key, value);
  }
  responseHeaders.set('cache-control', 'no-store');
  const response = new NextResponse(upstream.status === 204 ? null : upstream.body, {status: upstream.status, headers: responseHeaders});
  if (route === 'auth/logout' || (route === 'account/deactivate' && upstream.ok)) {
    response.cookies.delete('mediagent_access');
    response.cookies.delete('mediagent_refresh');
  }
  return response;
}

export {proxy as GET, proxy as POST};
