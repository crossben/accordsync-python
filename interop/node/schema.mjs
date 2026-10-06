import { conflict, counter, defineSchema, lww, set } from '@accordsync/server';

// The same schema the Python tests declare (interop/tests/support.py).
export const schema = defineSchema({
  dossier: {
    agent: lww(),
    zone: lww(),
    client_name: lww(),
    status: conflict(),
    visits: counter(),
    docs: set(),
  },
});
