// A TypeScript device for the mixed-server fleet: @accordsync/client from the Accord WORKSPACE
// (ACCORD_APP_DIR, sources), driven by the Python tests over stdin/stdout, one JSON line each.
// Every request goes to a server picked at random from `servers`, through a lossy network.
//
//   {"cmd":"open","deviceId":"ts-1","token":"…","servers":{"ts":"http://…","py":"http://…"},
//    "seed":7,"loss":0.25}
//   {"cmd":"assign"|"inc"|"add"|"remove","record":"…","field":"…","value":…}
//   {"cmd":"sync"} {"cmd":"loss","loss":0.25} {"cmd":"token","token":"…"}
//   {"cmd":"snapshot"} {"cmd":"has","record":"…"} {"cmd":"close"}
//
// Run with cwd = <accord app>/conformance:  node --import tsx --conditions=@accordsync/source …
import { createInterface } from 'node:readline';

const app = process.env.ACCORD_APP_DIR;
if (!app) throw new Error('ACCORD_APP_DIR is required');
const { AccordClient, httpTransport, MemoryStorage, conflict, counter, defineSchema, lww, set } =
  await import(`${app}/packages/client/src/index.ts`);
const { canonicalJson } = await import(`${app}/packages/core/src/index.ts`);

// The conformance profile's schema (python/tools/conformance_server.py).
const schema = defineSchema({
  dossier: {
    agent: lww(),
    zone: lww(),
    client_name: lww(),
    status: conflict(),
    visits: counter(),
    docs: set(),
  },
});

let client: any;
let loss = 0;
let token = '';
let servers: [string, string][] = [];
let rnd = () => 0;
// Requests that reached each server, by kind: { ts: { push: 3, pull: 5 }, py: … }.
const served: Record<string, Record<string, number>> = {};

function mulberry32(seed: number) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// Picks a server for each request; loses requests, and responses after the server applied them.
const fleetFetch = async (input: string | URL | Request, init?: RequestInit) => {
  const url = new URL(String(input));
  const [name, base] = servers[Math.floor(rnd() * servers.length)]!;
  if (rnd() < loss / 2) throw new TypeError('network down (request lost)');
  const kind = url.pathname.split('/').pop()!;
  served[name] ??= {};
  served[name][kind] = (served[name][kind] ?? 0) + 1;
  const res = await fetch(`${base}${url.pathname}${url.search}`, init);
  const body = await res.arrayBuffer();
  if (rnd() < loss / 2) throw new TypeError('network down (response lost)');
  return new Response(body, { status: res.status, headers: res.headers });
};

async function run(m: any): Promise<Record<string, unknown>> {
  switch (m.cmd) {
    case 'open':
      rnd = mulberry32(m.seed ?? 1);
      loss = m.loss ?? 0;
      token = m.token;
      servers = Object.entries(m.servers as Record<string, string>);
      client = await AccordClient.open({
        schema,
        storage: new MemoryStorage(),
        deviceId: m.deviceId,
        transport: httpTransport({ url: 'http://fleet', getToken: () => token, fetch: fleetFetch }),
      });
      return { deviceId: client.deviceId };
    case 'assign':
    case 'inc':
    case 'add':
    case 'remove': {
      const op = await client[m.cmd](m.record, m.field, m.value);
      return { opId: op.opId };
    }
    case 'sync':
      try {
        await client.sync();
        return { ok: true };
      } catch (e) {
        return { ok: false, error: String(e) };
      }
    case 'loss':
      loss = m.loss;
      return {};
    case 'token':
      token = m.token;
      return {};
    case 'has':
      return { has: client.records().includes(m.record) };
    case 'snapshot':
      return {
        snapshot: canonicalJson(
          Object.fromEntries(client.records().map((r: string) => [r, client.read(r)])),
        ),
        pending: client.status().pending,
        served,
      };
    case 'close':
      await client.close();
      setImmediate(() => process.exit(0));
      return {};
    default:
      throw new Error(`unknown command ${m.cmd}`);
  }
}

// Commands run one at a time, in order.
let chain = Promise.resolve();
createInterface({ input: process.stdin }).on('line', (line) => {
  chain = chain.then(async () => {
    try {
      process.stdout.write(`${JSON.stringify({ ok: true, ...(await run(JSON.parse(line))) })}\n`);
    } catch (e: any) {
      process.stdout.write(`${JSON.stringify({ ok: false, error: String(e?.stack ?? e) })}\n`);
    }
  });
});
