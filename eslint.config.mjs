// Minimal flat config. Deliberately not a style linter: prettier-style rules
// would produce a huge first diff across a 5,000-line renderer and catch
// nothing. These rules catch the defects that actually reach users — undefined
// variables, unreachable code, broken hook dependencies, accidental globals.
import js from "@eslint/js";
import react from "eslint-plugin-react";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";

export default [
  {
    ignores: [
      "dist/**",
      "release/**",
      "installer/**",
      "build/**",
      "node_modules/**",
      ".electron-data/**",
    ],
  },
  {
    files: ["src/**/*.{js,jsx}"],
    ...js.configs.recommended,
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: "module",
      globals: { ...globals.browser },
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    plugins: { react, "react-hooks": reactHooks },
    settings: { react: { version: "detect" } },
    rules: {
      ...js.configs.recommended.rules,
      "react/jsx-uses-react": "error",
      "react/jsx-uses-vars": "error",
      // The renderer imports React explicitly and does not use the new JSX
      // transform, so React must count as used.
      "no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^(React|_)" }],
      // Hook ordering is a real defect class — a conditional hook corrupts
      // state for every hook after it. Enforced.
      "react-hooks/rules-of-hooks": "error",
      // Advisory only: several effects intentionally run on a narrower
      // dependency list to avoid refresh loops.
      "react-hooks/exhaustive-deps": "warn",
      // The React Compiler rules shipped in the plugin's recommended preset
      // (set-state-in-effect, preserve-manual-memoization, and friends) are
      // optimisation advice, not defect detection. Turning them on would make
      // CI red on day one for ~20 non-defects, which trains people to ignore
      // it. Left off deliberately; revisit if the app adopts the compiler.
    },
  },
  {
    files: ["electron/**/*.cjs", "tools/**/*.cjs", "*.cjs"],
    ...js.configs.recommended,
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: "commonjs",
      globals: { ...globals.node },
    },
    rules: {
      ...js.configs.recommended.rules,
      "no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
    },
  },
];
