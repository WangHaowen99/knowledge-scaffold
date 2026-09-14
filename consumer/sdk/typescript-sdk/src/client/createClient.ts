// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import { OntoAgentClient } from './OntoAgentClient.js';
import type { ClientOptions } from '../types/common.js';

export const createOntoAgentClient = (options: ClientOptions): OntoAgentClient => {
  return new OntoAgentClient(options);
};
