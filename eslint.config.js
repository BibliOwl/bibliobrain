// ESLint flat configuration for the Bibliobrain static frontend.
//
// The frontend is a set of classic <script> files sharing one global scope
// (app.js, settings.js, knowledge-graph.js, ...). Cross-file globals are
// therefore declared below instead of being flagged by `no-undef`. The rule set
// focuses on real defects; stylistic rules are intentionally left out.
const js = require("@eslint/js");
const globals = require("globals");

// Functions and state shared between the classic scripts. Keep this list in
// sync with static/index.html script order when adding a new file.
const sharedGlobals = {
  state: "writable",
  t: "readonly",
  escapeHtml: "readonly",
  bindBbHandler: "readonly",
  DOMPurify: "readonly",
  marked: "readonly",
  katex: "readonly",
  renderMathInElement: "readonly",
};

module.exports = [
  {
    ignores: ["static/vendor/**", "node_modules/**", ".venv/**"],
  },
  js.configs.recommended,
  {
    files: ["static/js/**/*.js"],
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: "script",
      globals: {
        ...globals.browser,
        ...sharedGlobals,
      },
    },
    rules: {
      // Classic scripts define each other's globals; a symbol lookup across
      // files is not an error here. Real typos still surface at runtime and in
      // tests/test_frontend_consistency_ui.py.
      "no-undef": "off",
      "no-unused-vars": [
        "warn",
        {
          // Every top-level declaration belongs to the shared global scope of
          // the classic scripts and can be referenced by another file or by a
          // data-bb handler. Keep checking genuinely local declarations.
          vars: "local",
          args: "none",
          caughtErrors: "none",
          varsIgnorePattern: "^_",
        },
      ],
      "no-empty": ["error", { allowEmptyCatch: true }],
      "no-prototype-builtins": "off",
      "no-inner-declarations": "off",
      "no-useless-escape": "warn",
      "no-eval": "error",
      "no-implied-eval": "error",
      "no-new-func": "error",
      "no-var": "error",
      "prefer-const": ["warn", { destructuring: "all" }],
      // Reported as warnings until the existing code base has been reviewed
      // against them; promote to "error" once `npm run lint` is clean.
      eqeqeq: ["warn", "smart"],
      "no-case-declarations": "warn",
      "no-fallthrough": "warn",
      // Do not report intentional names matching browser globals (for example
      // `state`); declarations duplicated in our own code are still reported.
      "no-redeclare": ["warn", { builtinGlobals: false }],
      "no-constant-condition": "warn",
      "no-cond-assign": "warn",
    },
  },
  {
    files: ["eslint.config.js"],
    languageOptions: { sourceType: "module", globals: globals.node },
  },
];
