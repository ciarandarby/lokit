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
- canonical whole-document formatting, including lossless full-line comments;
- safe pre-save formatting through `textDocument/willSaveWaitUntil`; and
- block folding ranges.

Formatting is offered only for a successfully parsed, synchronized document.
Every full-line `#` comment is preserved exactly, including indentation and
trailing whitespace, while line endings and modeled fields are canonicalized.
Formatting requests verify the document generation, revision, and version again
before returning edits, so an edit computed for stale text is never applied.

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

The release-only LSP integration benchmark generates a large semantic fixture,
measures analysis and formatting through the actual JSON-RPC service, then runs
a rapid-edit stress test and verifies that the final diagnostics belong to the
latest version:

```sh
cargo test --release --locked --manifest-path tools/lokit-lsp/Cargo.toml \
  --test throughput -- --ignored --nocapture
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

Documents are retained, analyzed, and formatted up to 128 MiB by default. A
client can send `initializationOptions.maxDocumentBytes` to choose a limit from
1 MiB through 1 GiB. Documents above the configured limit are diagnosed but not
retained or parsed; a later full-content replacement within the limit
resynchronizes them. Individual parser lines are limited to 1 MiB, structural
nesting to 16 levels, diagnostics to 200, completion results to 128, symbols to
2,048, and folding ranges to 4,096. One change notification is limited to 4,096
edit entries and aggregate incremental-edit work equal to the larger of 16 MiB
or the configured document limit. This permits an ordinary edit anywhere in a
large retained document without allowing a multi-edit notification to perform
unbounded repeated scans. Invalid or over-budget edits are atomic: the stored
text remains unchanged and ranged changes are rejected until the client sends
one bounded full-content replacement. This prevents later ranges from being
interpreted against stale server text. Analysis runs on two background workers.
Pending work is coalesced by URI, and a single URI can never be parsed by more
than one worker at once, so rapid edits do not create an unbounded parse backlog.

## Visual Studio Code

VS Code needs a small client extension to launch an external LSP server. A
minimal wrapper is included in [`editors/vscode`](editors/vscode). It expects
`lokit-lsp` on `PATH` by default; the `lokit.server.path` setting can provide an
absolute binary path. `lokit.maxDocumentBytes` forwards the bounded document
limit during server initialization and takes effect after the server restarts.

For local installation:

```sh
cd tools/lokit-lsp/editors/vscode
npm ci --ignore-scripts
npm run package
code --install-extension lokit-language-client-0.1.0.vsix
```

Reload VS Code and open a `.lokit` file. This wrapper follows the official
[VS Code language-server extension model](https://code.visualstudio.com/api/language-extensions/language-server-extension-guide).
The wrapper contributes language-scoped defaults that select Lokit as the
formatter and turn on `editor.formatOnSave`; either can be overridden normally.

## Neovim 0.11+

Add this to `init.lua` after putting `lokit-lsp` on `PATH`:

```lua
vim.filetype.add({ extension = { lokit = "lokit" } })

vim.lsp.config("lokit_lsp", {
  cmd = { "lokit-lsp" },
  filetypes = { "lokit" },
  root_markers = { ".git" },
  init_options = { maxDocumentBytes = 128 * 1024 * 1024 },
})
vim.lsp.enable("lokit_lsp")

vim.api.nvim_create_autocmd("BufWritePre", {
  pattern = "*.lokit",
  callback = function(args)
    vim.lsp.buf.format({
      bufnr = args.buf,
      async = false,
      filter = function(client)
        return client.name == "lokit_lsp"
      end,
    })
  end,
})
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
auto-format = true
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
select `tools/lokit-lsp/editors/zed`. Zed currently enables format-on-save by
default and selects an available formatter automatically. The following
per-language settings pin that behavior explicitly:

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
