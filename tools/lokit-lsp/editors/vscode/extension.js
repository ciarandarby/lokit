"use strict";

const vscode = require("vscode");
const {
  LanguageClient,
  TransportKind,
} = require("vscode-languageclient/node");

let client;

function activate() {
  const command = vscode.workspace
    .getConfiguration("lokit")
    .get("server.path", "lokit-lsp");
  const serverOptions = {
    run: { command, args: [], transport: TransportKind.stdio },
    debug: { command, args: [], transport: TransportKind.stdio },
  };
  const clientOptions = {
    documentSelector: [{ scheme: "file", language: "lokit" }],
  };
  client = new LanguageClient(
    "lokit-lsp",
    "Lokit Language Server",
    serverOptions,
    clientOptions,
  );
  client.start();
}

async function deactivate() {
  if (client) {
    await client.stop();
  }
}

module.exports = { activate, deactivate };

