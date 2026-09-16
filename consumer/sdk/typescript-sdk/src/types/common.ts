// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
export type MaybePromise<T> = T | Promise<T>;

export type JsonPrimitive = string | number | boolean | null;

export type JsonValue = JsonPrimitive | JsonObject | JsonValue[];

export interface JsonObject {
  [key: string]: JsonValue;
}

export type QueryValue = string | number | boolean | null | undefined;

export type ValueProvider<T> = T | (() => MaybePromise<T>);

export interface ClientOptions {
  baseUrl: string;
  token?: ValueProvider<string | null | undefined>;
  workspaceId?: ValueProvider<string | null | undefined>;
  headers?: ValueProvider<HeadersInit | undefined>;
  fetch?: typeof globalThis.fetch;
}

export interface RequestOptions<TJson = unknown> {
  query?: Record<string, QueryValue>;
  headers?: HeadersInit;
  signal?: AbortSignal;
  json?: TJson;
  body?: BodyInit | null;
}
