// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
export const isMessageUpdateScopeCurrent = (updateEpoch, currentEpoch) => (
  Number(updateEpoch) === Number(currentEpoch)
);
