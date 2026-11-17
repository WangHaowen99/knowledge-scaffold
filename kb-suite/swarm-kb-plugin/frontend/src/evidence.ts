import { focusOf, type Focus } from './preview';
import { rpc } from './rpc';
import type { Locator } from './types';

type Evidence = {
  doc_id: string;
  text?: string | null;
  locators?: (Locator & { line_start?: number; line_end?: number; time_start?: number })[];
};

/**
 * Where an anchored passage is: the backend's evidence (contract 9.6) when it offers it, otherwise what the
 * hit or chunk itself says (its locators and line).
 */
export async function locate(
  anchorId: string | null,
  own: { locators?: Locator[] | null; line_start?: number | null },
  withEvidence: boolean,
): Promise<Focus> {
  if (anchorId && withEvidence) {
    try {
      return focusOf(await rpc.call<Evidence>('evidence.get', { anchor_id: anchorId }));
    } catch {
      // an anchor the backend no longer knows: the hit's own position still helps
    }
  }
  return focusOf({ locators: own.locators, line_start: own.line_start });
}
