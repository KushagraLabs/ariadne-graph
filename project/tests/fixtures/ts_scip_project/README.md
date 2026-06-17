# SCIP TypeScript fixture

This directory holds a minimal TypeScript project and a committed `index.scip`
protobuf file used by deterministic parser unit tests.

## Purpose

The parser unit tests (`tests/test_typescript/test_scip_bridge.py`) exercise
the vendored SCIP protobuf bindings and the `ScipIndexParser` against the
checked-in `index.scip`. They do **not** require `scip-typescript`, `node`, or
`npm` to run, so they are stable in CI.

## Regenerating `index.scip`

If the fixture source files change, regenerate the binary:

```bash
cd tests/fixtures/ts_scip_project
npm install --no-save typescript @sourcegraph/scip-typescript
npx scip-typescript index
rm -rf node_modules package-lock.json
```

The committed artifact should then be updated with the new `index.scip`.

## Fixture contents

- `src/animal.ts` — `Animal` interface with `sound()`.
- `src/dog.ts` — `Dog` class implementing `Animal`.
- `src/utils.ts` — `helper()` function.
- `src/barrel.ts` — barrel re-export of `helper as utilHelper`.
- `src/main.ts` — imports `Dog`, imports `utilHelper as bar` through the barrel,
  and calls both.

This exercises renamed imports, barrel re-exports, `implements`, method calls,
and constructor calls.
