#![forbid(unsafe_code)]

use zed_extension_api as zed;

struct LokitExtension;

impl zed::Extension for LokitExtension {
    fn new() -> Self {
        Self
    }

    fn language_server_command(
        &mut self,
        _: &zed::LanguageServerId,
        worktree: &zed::Worktree,
    ) -> zed::Result<zed::Command> {
        let command = worktree
            .which("lokit-lsp")
            .ok_or_else(|| "lokit-lsp was not found on PATH".to_owned())?;
        Ok(zed::Command {
            command,
            args: Vec::new(),
            env: worktree.shell_env(),
        })
    }
}

zed::register_extension!(LokitExtension);
