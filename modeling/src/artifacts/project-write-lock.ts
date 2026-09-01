import { AsyncLocalStorage } from 'node:async_hooks';

const projectQueues = new Map<string, Promise<void>>();
interface ProjectLockLease { active: boolean }
const heldProjects = new AsyncLocalStorage<ReadonlyMap<string, ProjectLockLease>>();

/** Serializes package and draft writes for one project while allowing nested store calls. */
export async function runProjectWriteExclusive<T>(
  projectId: string,
  task: () => Promise<T>,
): Promise<T> {
  const held = heldProjects.getStore();
  if (held?.get(projectId)?.active) return task();

  const previous = projectQueues.get(projectId) ?? Promise.resolve();
  let release: () => void = () => undefined;
  const gate = new Promise<void>((resolveGate) => { release = resolveGate; });
  const tail = previous.then(() => gate);
  projectQueues.set(projectId, tail);
  await previous;

  const lease: ProjectLockLease = { active: true };
  const nextHeld = new Map(held ?? []);
  nextHeld.set(projectId, lease);
  try {
    return await heldProjects.run(nextHeld, task);
  } finally {
    // Detached async work inherits the context, but must not bypass the queue after this task ends.
    lease.active = false;
    release();
    if (projectQueues.get(projectId) === tail) projectQueues.delete(projectId);
  }
}
