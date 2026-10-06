// Runs the TypeScript server's migrateToLatest (Accord workspace sources) on a database:
//   node --import tsx --conditions=@accordsync/source migrate.mts <database url>
// Run with cwd = <accord app>/conformance (for tsx). Needs ACCORD_APP_DIR.
const app = process.env.ACCORD_APP_DIR;
if (!app) throw new Error('ACCORD_APP_DIR is required');
const url = process.argv[2];
if (!url) throw new Error('usage: migrate.mts <database url>');
const { createDb } = await import(`${app}/packages/server/src/db.ts`);
const { migrateToLatest } = await import(`${app}/packages/server/src/migrate.ts`);
const db = createDb(url);
await migrateToLatest(db);
await db.destroy();
console.log('ok');
