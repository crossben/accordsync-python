/**
 * Runs the TypeScript server's own code from an Accord checkout, for the Python server's tests:
 *   migrate <database url>   migrateToLatest (Kysely), as `accord serve` does at startup
 *   hash                     reads wire ops (JSON array) on stdin, prints opHash of each
 * Run with cwd = <accord>/app/conformance (for tsx): node --import tsx --conditions=@accordsync/source
 */
const app = process.env.ACCORD_APP_DIR;
if (!app) throw new Error('ACCORD_APP_DIR is required');
const [cmd, arg] = process.argv.slice(2);
if (cmd === 'migrate') {
  const { createDb } = await import(`${app}/packages/server/src/db.ts`);
  const { migrateToLatest } = await import(`${app}/packages/server/src/migrate.ts`);
  const db = createDb(arg);
  await migrateToLatest(db);
  await db.destroy();
  console.log('ok');
} else if (cmd === 'hash') {
  const { opHash } = await import(`${app}/packages/server/src/sync.ts`);
  let input = '';
  for await (const chunk of process.stdin) input += chunk;
  console.log(JSON.stringify((JSON.parse(input) as unknown[]).map((op) => opHash(op as never))));
} else {
  throw new Error(`unknown command ${cmd}`);
}
