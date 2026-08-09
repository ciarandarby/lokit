# Lokit language server

`lokit-lsp` is the standalone Language Server Protocol implementation for
Lokit interchange (`.lokit`) files. It uses the same `lokit-format` Rust parser,
validator, source spans, and canonical writer as the Python package bindings.
It does not maintain a second interchange parser.

The server implements the stable capability-driven portion of LSP 3.17/3.18:

- incremental open/change/close synchronization with version checks;
- push diagnostics for syntax and inline-content validation, including document
  versions only when the client advertises support;
- UTF-8, UTF-16, and UTF-32 position encoding negotiation;
- context-aware fields, blocks, enum values, locales, and tag-reference
  completion;
- schema hover;
- hierarchical document symbols, with flat-symbol fallback for older clients;
- canonical whole-document formatting; and
- block folding ranges.

Formatting is offered only for a successfully parsed document. Files containing
source-only `#` comments are deliberately not rewritten because comments are
not part of `BaseStructure` and the canonical model writer cannot preserve them.

## Build and install

Rust 1.85 or newer is required for the language server. From the repository
root:

```sh
cargo build --release --locked --manifest-path tools/lokit-lsp/Cargo.toml
```

The binary is created at `tools/lokit-lsp/target/release/lokit-lsp` when the
crate uses its default target directory. Install it on `PATH` with:

```sh
cargo install --locked --path tools/lokit-lsp
```

Run its checks with:

```sh
cargo test --locked --manifest-path tools/lokit-lsp/Cargo.toml
cargo clippy --locked --manifest-path tools/lokit-lsp/Cargo.toml --all-targets -- -D warnings
```

## Transport and limits

Launch the server with no arguments. It speaks JSON-RPC/LSP over standard input
and output. Standard output is reserved exclusively for framed LSP messages;
the server installs no stdout logger and exposes no TCP or command-execution
transport.

Editor clients should register:

| Setting | Value |
| --- | --- |
| Command | `lokit-lsp` |
| Arguments | none |
| Transport | stdio |
| Language identifier | `lokit` |
| File pattern | `**/*.lokit` |
| Synchronization | incremental |

Documents larger than 16 MiB are diagnosed but not retained or parsed; a later
full-content replacement within the limit resynchronizes them. Individual
parser lines are limited to 1 MiB, structural nesting to 16 levels, diagnostics
to 200, completion results to 128, symbols to 2,048, and folding ranges to
4,096. One change notification is limited to 4,096 edit entries and 16 MiB of
aggregate incremental-edit work. Invalid or over-budget edits are atomic: the
stored text remains unchanged and ranged changes are rejected until the client
sends one bounded full-content replacement. This prevents later ranges from
being interpreted against stale server text.

## Visual Studio Code

VS Code needs a small client extension to launch an external LSP server. A
minimal wrapper is included in [`editors/vscode`](editors/vscode). It expects
`lokit-lsp` on `PATH` by default; the `lokit.server.path` setting can provide an
absolute binary path.

For local installation:

```sh
cd tools/lokit-lsp/editors/vscode
npm install
npx @vscode/vsce package
code --install-extension lokit-language-client-0.1.0.vsix
```

Reload VS Code and open a `.lokit` file. This wrapper follows the official
[VS Code language-server extension model](https://code.visualstudio.com/api/language-extensions/language-server-extension-guide).

## Neovim 0.11+

Add this to `init.lua` after putting `lokit-lsp` on `PATH`:

```lua
vim.filetype.add({ extension = { lokit = "lokit" } })

vim.lsp.config("lokit_lsp", {
  cmd = { "lokit-lsp" },
  filetypes = { "lokit" },
  root_markers = { ".git" },
})
vim.lsp.enable("lokit_lsp")
```

Use `:checkhealth vim.lsp` to verify attachment. The configuration uses
Neovim's documented [`vim.lsp.config` and `vim.lsp.enable`](https://neovim.io/doc/user/lsp)
interfaces.

## Helix

Add the following to `~/.config/helix/languages.toml`:

```toml
[language-server.lokit-lsp]
command = "lokit-lsp"

[[language]]
name = "lokit"
scope = "source.lokit"
file-types = ["lokit"]
comment-token = "#"
indent = { tab-width = 2, unit = "  " }
language-servers = ["lokit-lsp"]
```

If Helix requires a grammar for an otherwise unknown language in the installed
version, add a local grammar or temporarily set `grammar = "json"`; the
language server remains responsible for `.lokit` validation and formatting.
See the official [Helix language configuration](https://docs.helix-editor.com/languages.html).

## Zed

Zed requires a language extension to register an otherwise unknown language;
settings alone cannot attach a server to a new `.lokit` language. A local
development wrapper is included at [`editors/zed`](editors/zed). It registers
the extension, finds `lokit-lsp` on the worktree environment's `PATH`, and
registers the pinned `tree-sitter-json` grammar under its actual `json` export
name for baseline string/bracket highlighting. The shared Lokit parser remains
authoritative for syntax, validation, and formatting.

Build and install `lokit-lsp`, then in Zed run **zed: install dev extension** and
select `tools/lokit-lsp/editors/zed`. Optional per-language settings are:

```json
{
  "languages": {
    "Lokit": {
      "tab_size": 2,
      "hard_tabs": false,
      "formatter": "language_server",
      "format_on_save": "on"
    }
  },
  "lsp": {
    "lokit-lsp": {
      "binary": {
        "path": "lokit-lsp",
        "arguments": []
      }
    }
  }
}
```

Zed documents this workflow in its
[language-extension guide](https://zed.dev/docs/extensions/languages) and
[development-extension instructions](https://zed.dev/docs/extensions/developing-extensions).
