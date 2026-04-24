import js from '@eslint/js'
import globals from 'globals'
import reactPlugin from 'eslint-plugin-react'
import reactHooks from 'eslint-plugin-react-hooks'

export default [
    js.configs.recommended,
    {
        files: ['src/**/*.{js,jsx}'],
        plugins: {
            react: reactPlugin,
            'react-hooks': reactHooks,
        },
        languageOptions: {
            ecmaVersion: 2022,
            sourceType: 'module',
            globals: {
                ...globals.browser,
            },
            parserOptions: {
                ecmaFeatures: { jsx: true },
            },
        },
        settings: {
            react: { version: '18' },
        },
        rules: {
            ...reactPlugin.configs.recommended.rules,
            ...reactHooks.configs.recommended.rules,
            // nie wymuszaj PropTypes — projekt nie używa
            'react/prop-types': 'off',
            // JSX nie wymaga importu React (React 17+)
            'react/react-in-jsx-scope': 'off',
            // bez console.log w prod (warn żeby nie blokować w dev)
            'no-console': ['warn', { allow: ['warn', 'error'] }],
            // nieużywane zmienne są błędem
            'no-unused-vars': ['error', { varsIgnorePattern: '^_', argsIgnorePattern: '^_' }],
        },
    },
    {
        ignores: ['dist/**', 'node_modules/**'],
    },
]
