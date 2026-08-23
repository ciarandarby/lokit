use std::error::Error;
use std::io::{self, BufRead, BufReader, Write};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::time::{Duration, Instant};

use serde_json::{Value, json};

const EXIT_TIMEOUT: Duration = Duration::from_secs(5);
const POLL_INTERVAL: Duration = Duration::from_millis(10);

struct ServerProcess {
    child: Child,
}

impl ServerProcess {
    fn spawn() -> Result<Self, io::Error> {
        let child = Command::new(env!("CARGO_BIN_EXE_lokit-lsp"))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()?;
        Ok(Self { child })
    }

    fn wait_for_exit(&mut self) -> Result<ExitStatus, io::Error> {
        let started = Instant::now();
        loop {
            if let Some(status) = self.child.try_wait()? {
                return Ok(status);
            }
            if started.elapsed() >= EXIT_TIMEOUT {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "lokit-lsp did not exit after the LSP exit notification",
                ));
            }
            std::thread::sleep(POLL_INTERVAL);
        }
    }
}

impl Drop for ServerProcess {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[test]
#[allow(clippy::too_many_lines)]
fn binary_stdio_round_trip_formats_on_save_and_shuts_down_cleanly() -> Result<(), Box<dyn Error>> {
    let mut server = ServerProcess::spawn()?;
    let stdin = server
        .child
        .stdin
        .take()
        .ok_or_else(|| io::Error::other("lokit-lsp stdin was not piped"))?;
    let stdout = server
        .child
        .stdout
        .take()
        .ok_or_else(|| io::Error::other("lokit-lsp stdout was not piped"))?;
    let mut writer = stdin;
    let mut reader = BufReader::new(stdout);

    write_message(
        &mut writer,
        &json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "capabilities": {
                    "textDocument": {
                        "semanticTokens": {
                            "requests": {"range": true, "full": true},
                            "tokenTypes": [
                                "keyword",
                                "property",
                                "string",
                                "number",
                                "enumMember",
                                "comment",
                                "operator"
                            ],
                            "tokenModifiers": [],
                            "formats": ["relative"]
                        }
                    }
                }
            }
        }),
    )?;
    let initialize = read_message(&mut reader)?;
    assert_eq!(initialize["id"], 1);
    assert_eq!(
        initialize["result"]["capabilities"]["documentFormattingProvider"],
        true
    );
    assert_eq!(
        initialize["result"]["capabilities"]["textDocumentSync"]["willSaveWaitUntil"],
        true
    );
    assert_eq!(
        initialize["result"]["capabilities"]["semanticTokensProvider"]["full"],
        true
    );

    let source = concat!(
        "# retain this comment  \n",
        "@lokit 1\n",
        "document {\n",
        "  target_locale = \"fr\"\n",
        "  source_locale = \"en\"\n",
        "}\n",
        "unit \"hello\" {\n",
        "  source = \"Hello\"\n",
        "  target = \"Bonjour\"\n",
        "}\n",
    );
    write_message(
        &mut writer,
        &json!({"jsonrpc": "2.0", "method": "initialized", "params": {}}),
    )?;
    write_message(
        &mut writer,
        &json!({
            "jsonrpc": "2.0",
            "method": "textDocument/didOpen",
            "params": {
                "textDocument": {
                    "uri": "file:///integration/stdio.lokit",
                    "languageId": "lokit",
                    "version": 1,
                    "text": source
                }
            }
        }),
    )?;
    write_message(
        &mut writer,
        &json!({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "textDocument/willSaveWaitUntil",
            "params": {
                "textDocument": {"uri": "file:///integration/stdio.lokit"},
                "reason": 1
            }
        }),
    )?;

    let mut formatted = String::new();
    let mut saw_diagnostics = false;
    for _ in 0..4 {
        let message = read_message(&mut reader)?;
        if message["id"] == 2 {
            formatted = message["result"][0]["newText"]
                .as_str()
                .ok_or_else(|| io::Error::other("will-save response omitted its formatting edit"))?
                .to_owned();
        } else if message["method"] == "textDocument/publishDiagnostics" {
            assert_eq!(message["params"]["uri"], "file:///integration/stdio.lokit");
            assert_eq!(message["params"]["diagnostics"], json!([]));
            saw_diagnostics = true;
        }
        if !formatted.is_empty() && saw_diagnostics {
            break;
        }
    }
    assert!(saw_diagnostics);
    assert!(formatted.starts_with("# retain this comment  \n@lokit 1\n"));
    assert_eq!(
        lokit_format::parse_str(&formatted)?,
        lokit_format::parse_str(source)?
    );

    write_message(
        &mut writer,
        &json!({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "textDocument/semanticTokens/range",
            "params": {
                "textDocument": {"uri": "file:///integration/stdio.lokit"},
                "range": {
                    "start": {"line": 1, "character": 0},
                    "end": {"line": 1, "character": 8}
                }
            }
        }),
    )?;
    loop {
        let message = read_message(&mut reader)?;
        if message["id"] == 3 {
            assert_eq!(
                message["result"]["data"],
                json!([1, 0, 6, 0, 0, 0, 7, 1, 3, 0])
            );
            break;
        }
    }
    write_message(
        &mut writer,
        &json!({"jsonrpc": "2.0", "id": 4, "method": "shutdown", "params": null}),
    )?;
    loop {
        let message = read_message(&mut reader)?;
        if message["id"] == 4 {
            assert!(message["result"].is_null());
            break;
        }
    }
    write_message(
        &mut writer,
        &json!({"jsonrpc": "2.0", "method": "exit", "params": null}),
    )?;
    drop(writer);
    assert!(server.wait_for_exit()?.success());
    Ok(())
}

fn write_message(writer: &mut impl Write, message: &Value) -> Result<(), Box<dyn Error>> {
    let body = serde_json::to_vec(message)?;
    write!(writer, "Content-Length: {}\r\n\r\n", body.len())?;
    writer.write_all(&body)?;
    writer.flush()?;
    Ok(())
}

fn read_message(reader: &mut impl BufRead) -> Result<Value, Box<dyn Error>> {
    let mut content_length = None;
    loop {
        let mut header = String::new();
        if reader.read_line(&mut header)? == 0 {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "lokit-lsp closed stdout before sending a complete response",
            )
            .into());
        }
        let header = header.trim_end_matches(['\r', '\n']);
        if header.is_empty() {
            break;
        }
        if let Some(value) = header.strip_prefix("Content-Length:") {
            content_length = Some(value.trim().parse::<usize>()?);
        }
    }
    let length = content_length.ok_or_else(|| {
        io::Error::new(io::ErrorKind::InvalidData, "missing Content-Length header")
    })?;
    let mut body = vec![0_u8; length];
    reader.read_exact(&mut body)?;
    Ok(serde_json::from_slice(&body)?)
}
