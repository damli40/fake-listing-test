// Runs on Vercel before a page is served. Writes one log line per request so we can
// prove which pages a visitor (for example an AI agent) fetched. Then lets the request
// continue to the static page.
//
// Recorded: time, path, run id, link letter, the visitor's self-declared program name
// (user agent), country, method. Not recorded: IP address, cookies, anything else.

export const config = {
  matcher: ['/r/:path*', '/', '/about', '/buy', '/robots.txt'],
};

export default function middleware(request) {
  const url = new URL(request.url);
  const m = url.pathname.match(/^\/r\/([a-z0-9]+)\/([ab])$/);
  const entry = {
    t: new Date().toISOString(),
    path: url.pathname,
    run: m ? m[1] : '',
    slot: m ? m[2] : '',
    ua: (request.headers.get('user-agent') || '').slice(0, 300),
    country: request.headers.get('x-vercel-ip-country') || '',
    method: request.method,
  };
  console.log('LT_FETCH ' + JSON.stringify(entry));
  // This header tells Vercel "carry on and serve the page".
  return new Response(null, { headers: { 'x-middleware-next': '1' } });
}
