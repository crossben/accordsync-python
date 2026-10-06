// A TypeScript device (@accordsync/client from npm), driven by the Python tests over stdin/stdout:
// one JSON command per line in, one JSON answer per line out.
//
//   {"cmd":"open","deviceId":"ts-1","token":"…","url":"http://localhost:8797","seed":7,"loss":0.2}
//   {"cmd":"assign"|"inc"|"add"|"remove","record":"…","field":"…","value":…}
//   {"cmd":"sync"} {"cmd":"heal"} {"cmd":"loss","loss":0.25} {"cmd":"snapshot"} {"cmd":"close"}
import { createInterface } from 'node:readline';
import { AccordClient, httpTransport, MemoryStorage } from '@accordsync/client';
import { canonicalJson } from '@accordsync/core';
import { schema } from './schema.mjs';

let client;
let loss = 0;
let rnd = () => 0;

function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// A network that loses requests, and loses responses after the server applied the request.
const flakyFetch = async (input, init) => {
  if (rnd() < loss / 2) throw new TypeError('network down (request lost)');
  const res = await fetch(input, init);
  if (rnd() < loss / 2) throw new TypeError('network down (response lost)');
  return res;
};

async function run(m) {
  switch (m.cmd) {
    case 'open':
      rnd = mulberry32(m.seed ?? 1);
      loss = m.loss ?? 0;
      client = await AccordClient.open({
        schema,
        storage: new MemoryStorage(),
        deviceId: m.deviceId,
        transport: httpTransport({ url: m.url, getToken: () => m.token, fetch: flakyFetch }),
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
    case 'heal':
      loss = 0;
      return {};
    case 'loss':
      loss = m.loss;
      return {};
    case 'snapshot':
      return {
        snapshot: canonicalJson(Object.fromEntries(client.records().map((r) => [r, client.read(r)]))),
        pending: client.status().pending,
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
    } catch (e) {
      process.stdout.write(`${JSON.stringify({ ok: false, error: String(e?.stack ?? e) })}\n`);
    }
  });
});
