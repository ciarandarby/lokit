# Lokit language client

This Visual Studio Code extension starts the standalone `lokit-lsp` executable
for `.lokit` localization interchange files. Install `lokit-lsp` on `PATH`, or
set **Lokit: Server Path** to its absolute path.

The server accepts files up to 128 MiB by default. **Lokit: Max Document
Bytes** can set a 1 MiB to 1 GiB workspace-specific limit; restart the language
server after changing it.

The server provides diagnostics, completion, hover, document symbols, folding,
and canonical document formatting. This extension makes Lokit the default
formatter for `.lokit` files and enables format-on-save for the language. Both
settings remain ordinary language-scoped VS Code settings and can be overridden
in user or workspace configuration.
