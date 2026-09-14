// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import { randomBytes } from 'node:crypto';

export const SYNTHETIC_ACCOUNT = Object.freeze({
  username: 'operator_a',
  email: 'user@example.com',
  password: `Aa1!${randomBytes(18).toString('base64url')}`,
});
