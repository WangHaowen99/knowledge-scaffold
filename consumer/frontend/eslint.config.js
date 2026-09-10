import js from '@eslint/js';
import globals from 'globals';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';
import { defineConfig, globalIgnores } from 'eslint/config';

export default defineConfig([
  globalIgnores(['dist']),
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
      curly: ['error', 'all'],
      'consistent-return': 'error',
      indent: ['error', 2, { SwitchCase: 1 }],
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
