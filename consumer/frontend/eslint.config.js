// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import js from '@eslint/js';
import globals from 'globals';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';
import { defineConfig, globalIgnores } from 'eslint/config';

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['scripts/*.mjs'],
    rules: {
      camelcase: [
        'error',
        {
          properties: 'never',
          ignoreDestructuring: false,
          ignoreImports: false,
          ignoreGlobals: false,
        },
      ],
      'max-len': ['error', { code: 120, tabWidth: 2, ignoreUrls: true, ignoreRegExpLiterals: true }],
      'no-console': 'error',
      'operator-linebreak': ['error', 'after'],
    },
  },
  {
    files: ['**/*.{js,jsx}'],
    extends: [
      js.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
      parserOptions: {
        ecmaVersion: 'latest',
        ecmaFeatures: { jsx: true },
        sourceType: 'module',
      },
    },
    rules: {
      'block-spacing': ['error', 'always'],
      'brace-style': ['error', '1tbs', { allowSingleLine: true }],
      camelcase: [
        'error',
        {
          properties: 'never',
          ignoreDestructuring: false,
          ignoreImports: false,
          ignoreGlobals: false,
        },
      ],
      complexity: ['error', 20],
      curly: ['error', 'all'],
      'consistent-return': 'error',
      'default-param-last': 'error',
      indent: ['error', 2, { SwitchCase: 1 }],
      'max-len': ['error', { code: 120, tabWidth: 2, ignoreUrls: true, ignoreRegExpLiterals: true }],
      'max-lines-per-function': [
        'error',
        { max: 100, skipBlankLines: true, skipComments: true, IIFEs: true },
      ],
      'no-alert': 'error',
      'no-console': 'error',
      'no-nested-ternary': 'error',
      'no-shadow': 'error',
      'no-trailing-spaces': 'error',
      'no-unused-vars': ['error', { varsIgnorePattern: '^[A-Z_]' }],
      'object-curly-newline': [
        'error',
        {
          ObjectExpression: {
            multiline: true,
            minProperties: 5,
            consistent: true,
          },
        },
      ],
      'operator-linebreak': ['error', 'after'],
      'prefer-destructuring': ['error', { array: true, object: true }],
      'prefer-named-capture-group': 'error',
      semi: ['error', 'always'],
    },
  },
]);
