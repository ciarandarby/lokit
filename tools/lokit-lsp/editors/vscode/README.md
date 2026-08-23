# Lokit language client

This Visual Studio Code extension starts the standalone `lokit-lsp` executable
for `.lokit` localization interchange files. Install `lokit-lsp` on `PATH`, or
set **Lokit: Server Path** to its absolute path.

The server provides diagnostics, completion, hover, document symbols, folding,
negotiated semantic highlighting, and canonical document formatting. Valid
documents are retained without a fixed file-size cutoff. This extension makes
Lokit the default formatter for `.lokit` files and enables format-on-save and
semantic highlighting for the language. These remain ordinary language-scoped
VS Code settings and can be overridden in user or workspace configuration.
