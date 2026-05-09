# Repository Guidelines

## Project Structure & Module Organization
The conversion logic lives in `alipay.js` and `wechat.js`, each exporting a small CLI that normalizes downloaded statements into MoneyWiz-ready CSV rows. Shared configuration sits in `account_map.json`; update its key/value pairs to map raw payee strings to your MoneyWiz account names before running conversions. Generated files are written beside the source statement and use the `【生成】...` naming scheme. `node_modules/` is standard npm output and should remain untracked by commits.

## Build, Test, and Development Commands
Use Node.js ≥14.16.1 (`nvm install lts/fermium && nvm use lts/fermium`) and install dependencies once with `npm install`. Run `npm run alipay` or `npm run wechat` to launch the guided transformers; both commands request a source file path and produce a localized CSV in-place. During development you can execute `node alipay.js` or `node wechat.js` directly for quicker iteration.

## Coding Style & Naming Conventions
Code is written as ES modules—prefer `import`/`export` syntax and keep side effects inside `main()` flows. Follow the existing two-space indentation, favor `const` over `let`, and keep helpers pure and reusable. Use lower camelCase for functions (`mainProcess`) and descriptive uppercase for shared constants (`ACCOUNT_MAP`). Strings that convey text should use single quotes unless template literals improve readability.

## Testing Guidelines
Automated tests are not yet in place, so validate changes by running the relevant npm script against masked sample statements. Inspect the resulting CSV headers (`账户, 转账, 描述, ...`) and spot-check a few rows to confirm date formatting, sign conventions, and mapped accounts. When adding new parsing rules, capture before/after examples in the PR description and consider contributing anonymized fixtures for regression checks.

## Commit & Pull Request Guidelines
Commit messages follow Conventional Commits (`feat(scope): ...`, `fix: ...`); match that style and group unrelated changes into separate commits. Pull requests should explain the problem, outline the solution, and mention manual verification steps or sample outputs. Link related issues when available and attach screenshots or CSV snippets if the change alters user-visible output.

## Configuration Tips
Keep a personal copy of `account_map.json` outside version control if it includes sensitive data, and document any new mapping keys in the PR so collaborators understand the intent. For regional statement variants, call out required locale settings or extra decoder steps in both this guide and the README.
