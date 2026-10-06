// The real Accord server (@accordsync/server from npm) on PostgreSQL, plus test-only routes on a
// second port: sign a token, reset the database, run compaction. Never expose the test port.
//
//   ACCORD_DATABASE_URL=postgres://… ACCORD_PORT=8797 ACCORD_TEST_PORT=8798 node server.mjs
import { createServer } from 'node:http';
import { serve } from '@hono/node-server';
import { compact, createApp, createDb, defineServer, migrateToLatest } from '@accordsync/server';
import { SignJWT } from 'jose';
import { sql } from 'kysely';
import { schema } from './schema.mjs';

const SECRET = 'interop-secret-at-least-32-bytes-long!!';
const port = Number(process.env.ACCORD_PORT ?? 8797);
const testPort = Number(process.env.ACCORD_TEST_PORT ?? 8798);

const key = (prefix, v) => (typeof v === 'string' ? [`${prefix}:${v}`] : []);
const def = defineServer({
  schema,
  scopes: { dossier: (r) => [...key('agent', r.fields.agent), ...key('zone', r.fields.zone)] },
  access: (c) => {
    const keys = [`agent:${c.sub}`, ...(Array.isArray(c.zones) ? c.zones : []).map((z) => `zone:${z}`)];
    return { read: keys, write: keys };
  },
  auth: { hs256Secret: SECRET },
  compaction: { minOps: 2, intervalMs: 0 },
});

const db = createDb(process.env.ACCORD_DATABASE_URL);
await migrateToLatest(db);
serve({ fetch: createApp({ db, def }).fetch, port });

createServer(async (req, res) => {
  const url = new URL(req.url, 'http://test');
  try {
    let body;
    if (url.pathname === '/token') {
      body = {
        token: await new SignJWT({ zones: url.searchParams.getAll('zone') })
          .setProtectedHeader({ alg: 'HS256' })
          .setSubject(url.searchParams.get('sub'))
          .setExpirationTime('1h')
          .sign(new TextEncoder().encode(SECRET)),
      };
    } else if (url.pathname === '/reset') {
      await sql`truncate feed, records, devices, compacted_ops restart identity`.execute(db);
      body = {};
    } else if (url.pathname === '/compact') {
      body = await compact(db, def);
    } else {
      res.writeHead(404).end();
      return;
    }
    res.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify(body));
  } catch (e) {
    res.writeHead(500).end(String(e?.stack ?? e));
  }
}).listen(testPort, () => console.log(`interop server on :${port}, test routes on :${testPort}`));
