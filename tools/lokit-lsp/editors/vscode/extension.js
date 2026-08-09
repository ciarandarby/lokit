"use strict";

const vscode = require("vscode");
const {
  LanguageClient,
  TransportKind,
} = require("vscode-languageclient/node");

let client;

async function activate() {
  const configuration = vscode.workspace.getConfiguration("lokit");
  const command = configuration.get("server.path", "lokit-lsp");
  const maxDocumentBytes = configuration.get(
    "maxDocumentBytes",
    128 * 1024 * 1024,
  );
  const serverOptions = {
    run: { command, args: [], transport: TransportKind.stdio },
    debug: { command, args: [], transport: TransportKind.stdio },
  };
  const clientOptions = {
    documentSelector: [{ language: "lokit" }],
    initializationOptions: { maxDocumentBytes },
  };
  client = new LanguageClient(
    "lokit-lsp",
    "Lokit Language Server",
    serverOptions,
    clientOptions,
  );
  await client.start();
}

async function deactivate() {
  if (client) {
    await client.stop();
  }
}

module.exports = { activate, deactivate };
