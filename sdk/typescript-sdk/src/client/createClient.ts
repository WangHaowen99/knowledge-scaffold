import { OntoAgentClient } from './OntoAgentClient.js';
import type { ClientOptions } from '../types/common.js';

export const createOntoAgentClient = (options: ClientOptions): OntoAgentClient => {
  return new OntoAgentClient(options);
};
