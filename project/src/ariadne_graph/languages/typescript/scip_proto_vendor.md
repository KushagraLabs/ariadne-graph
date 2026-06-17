# Vendored SCIP protobuf bindings

This directory contains generated Python protobuf bindings for the Sourcegraph
Code Intelligence Protocol (SCIP). The generated module is checked into the
repository so that the parser unit tests are deterministic and do not require
`protoc` or a network connection at test time.

## Source

- **Repository:** `scip-code/scip` (upstream moved from `sourcegraph/scip`)
- **Pinned commit:** `3b30443d39f2ad9a9f1b3c2dd770893022e81172`
- **Commit date:** 2026-06-05
- **Source file:** `scip.proto`
- **Permalink:**
  https://github.com/scip-code/scip/blob/3b30443d39f2ad9a9f1b3c2dd770893022e81172/scip.proto

## Why this commit?

This is the latest commit that modified `scip.proto` at the time the bindings
were generated. It introduced typed `SingleLineRange` and `MultiLineRange`
messages (and corresponding `typed_range` / `typed_enclosing_range` oneof
fields) while keeping the deprecated `range` / `enclosing_range` fields for
backward compatibility with `scip-typescript@0.4.0`.

## Generation command

```bash
cd project
curl -L -o /tmp/scip.proto \
  https://raw.githubusercontent.com/scip-code/scip/3b30443d39f2ad9a9f1b3c2dd770893022e81172/scip.proto
python -m grpc_tools.protoc \
  --python_out=src/ariadne_graph/languages/typescript \
  --proto_path=/tmp scip.proto
mv src/ariadne_graph/languages/typescript/scip_pb2.py \
   src/ariadne_graph/languages/typescript/_scip_pb2.py
```

## Runtime requirements

The generated bindings require `protobuf>=6.30`. The stub embeds a
`ValidateProtobufRuntimeVersion(PUBLIC, 6, 33, 5, ...)` gen-runtime gate and
imports `google.protobuf.runtime_version` (added in protobuf 5.26), so older
runtimes fail at import. They were generated with:

- `grpcio-tools==1.81.1`
- `protobuf==6.33.6`

## Regenerating

If the upstream `scip.proto` changes in a way that affects parsing, or if the
project's `protobuf` major version changes, regenerate the bindings using the
command above and update this file with the new commit hash and generation
tool versions.
