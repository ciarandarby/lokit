use std::collections::HashMap;
use std::sync::Arc;

use lokit_format::ParsedDocument;
use tokio::sync::{Mutex, RwLock};
use tower_lsp_server::jsonrpc::{Error, Result};
use tower_lsp_server::ls_types::{
    CompletionItemKind, CompletionOptions, CompletionParams, CompletionResponse, Diagnostic,
    DiagnosticSeverity, DidChangeTextDocumentParams, DidCloseTextDocumentParams,
    DidOpenTextDocumentParams, DocumentFormattingParams, DocumentSymbolParams,
    DocumentSymbolResponse, FoldingRangeParams, FoldingRangeProviderCapability, Hover, HoverParams,
    HoverProviderCapability, InitializeParams, InitializeResult, InitializedParams, MarkupKind,
    NumberOrString, OneOf, Range, ServerCapabilities, ServerInfo, SymbolKind,
    TextDocumentSyncCapability, TextDocumentSyncKind, TextDocumentSyncOptions, TextEdit, Uri,
};
use tower_lsp_server::{Client, LanguageServer};

use crate::analysis::{
    DocumentAnalysis, analyze_document, canonical_text, completions, contains_source_comments,
    document_symbols, folding_ranges, hover,
};
use crate::document::{ChangeError, Document, LineIndex, PositionEncoding};

const MAX_DOCUMENT_BYTES: usize = 16 * 1024 * 1024;

#[derive(Clone)]
struct DocumentSnapshot {
    text: Arc<str>,
    line_index: Arc<LineIndex>,
    generation: u64,
    revision: u64,
    version: i32,
    parsed: Option<Arc<ParsedDocument>>,
    oversized: bool,
    desynchronized: bool,
}

struct StoredDocument {
    source: Document,
    generation: u64,
    revision: u64,
    parsed: Option<Arc<ParsedDocument>>,
    oversized: bool,
    desynchronized: bool,
}

struct DocumentStore {
    documents: HashMap<Uri, StoredDocument>,
    next_generation: u64,
}

impl Default for DocumentStore {
    fn default() -> Self {
        Self {
            documents: HashMap::new(),
            next_generation: 1,
        }
    }
}

impl DocumentStore {
    fn open(&mut self, uri: Uri, text: String, version: i32) -> u64 {
        let oversized = text.len() > MAX_DOCUMENT_BYTES;
        let retained_text = if oversized { String::new() } else { text };
        let generation = self.next_generation;
        self.next_generation = self.next_generation.wrapping_add(1).max(1);
        self.documents.insert(
            uri,
            StoredDocument {
                source: Document::new(retained_text, version),
                generation,
                revision: 0,
                parsed: None,
                oversized,
                desynchronized: oversized,
            },
        );
        generation
    }

    fn change(
        &mut self,
        uri: &Uri,
        changes: &[tower_lsp_server::ls_types::TextDocumentContentChangeEvent],
        version: i32,
        encoding: PositionEncoding,
    ) -> std::result::Result<Option<DocumentSnapshot>, ChangeError> {
        let Some(document) = self.documents.get_mut(uri) else {
            return Ok(None);
        };
        if version <= document.source.version() {
            return Err(ChangeError::StaleVersion {
                current: document.source.version(),
                received: version,
            });
        }
        if document.desynchronized && changes.first().is_none_or(|change| change.range.is_some()) {
            document.revision = document.revision.wrapping_add(1);
            return Err(ChangeError::ResyncRequired);
        }
        let result = document
            .source
            .apply_changes(changes, version, encoding, MAX_DOCUMENT_BYTES);
        if let Err(error) = result {
            if !matches!(error, ChangeError::StaleVersion { .. }) {
                document.revision = document.revision.wrapping_add(1);
                document.parsed = None;
                document.desynchronized = true;
                document.oversized = matches!(error, ChangeError::DocumentTooLarge { .. });
            }
            return Err(error);
        }
        document.revision = document.revision.wrapping_add(1);
        document.parsed = None;
        document.oversized = false;
        document.desynchronized = false;
        Ok(Some(DocumentSnapshot {
            text: document.source.shared_text(),
            line_index: document.source.shared_line_index(),
            generation: document.generation,
            revision: document.revision,
            version: document.source.version(),
            parsed: None,
            oversized: false,
            desynchronized: false,
        }))
    }

    fn close(&mut self, uri: &Uri) -> bool {
        self.documents.remove(uri).is_some()
    }

    fn snapshot(&self, uri: &Uri) -> Option<DocumentSnapshot> {
        let document = self.documents.get(uri)?;
        Some(DocumentSnapshot {
            text: document.source.shared_text(),
            line_index: document.source.shared_line_index(),
            generation: document.generation,
            revision: document.revision,
            version: document.source.version(),
            parsed: document.parsed.clone(),
            oversized: document.oversized,
            desynchronized: document.desynchronized,
        })
    }

    fn install_analysis(
        &mut self,
        uri: &Uri,
        generation: u64,
        revision: u64,
        version: i32,
        parsed: Option<Arc<ParsedDocument>>,
    ) -> bool {
        let Some(document) = self.documents.get_mut(uri) else {
            return false;
        };
        if document.generation != generation
            || document.revision != revision
            || document.source.version() != version
        {
            return false;
        }
        if document.desynchronized && parsed.is_some() {
            return false;
        }
        document.parsed = parsed;
        true
    }

    fn identity(&self, uri: &Uri) -> Option<(u64, u64, i32)> {
        self.documents.get(uri).map(|document| {
            (
                document.generation,
                document.revision,
                document.source.version(),
            )
        })
    }
}

#[derive(Clone, Debug)]
struct ClientPreferences {
    encoding: PositionEncoding,
    hierarchical_symbols: bool,
    hover_markup: MarkupKind,
    folding_collapsed_text: bool,
    folding_range_limit: Option<u32>,
    diagnostics_version: bool,
    completion_kinds: Option<Vec<CompletionItemKind>>,
    symbol_kinds: Option<Vec<SymbolKind>>,
}

impl Default for ClientPreferences {
    fn default() -> Self {
        Self {
            encoding: PositionEncoding::Utf16,
            hierarchical_symbols: false,
            hover_markup: MarkupKind::PlainText,
            folding_collapsed_text: false,
            folding_range_limit: None,
            diagnostics_version: false,
            completion_kinds: None,
            symbol_kinds: None,
        }
    }
}

impl ClientPreferences {
    fn negotiate(params: &InitializeParams) -> Self {
        let text_document = params.capabilities.text_document.as_ref();
        let offered_encodings = params
            .capabilities
            .general
            .as_ref()
            .and_then(|general| general.position_encodings.as_deref());
        let document_symbol = text_document.and_then(|document| document.document_symbol.as_ref());
        let hover_markup = text_document
            .and_then(|document| document.hover.as_ref())
            .and_then(|hover| hover.content_format.as_deref())
            .and_then(|formats| {
                formats
                    .iter()
                    .find(|format| {
                        **format == MarkupKind::Markdown || **format == MarkupKind::PlainText
                    })
                    .cloned()
            })
            .unwrap_or(MarkupKind::PlainText);
        let folding = text_document.and_then(|document| document.folding_range.as_ref());
        Self {
            encoding: PositionEncoding::select(offered_encodings),
            hierarchical_symbols: document_symbol
                .and_then(|symbols| symbols.hierarchical_document_symbol_support)
                .unwrap_or(false),
            hover_markup,
            folding_collapsed_text: folding
                .and_then(|capability| capability.folding_range.as_ref())
                .and_then(|range| range.collapsed_text)
                .unwrap_or(false),
            folding_range_limit: folding.and_then(|capability| capability.range_limit),
            diagnostics_version: text_document
                .and_then(|document| document.publish_diagnostics.as_ref())
                .and_then(|diagnostics| diagnostics.version_support)
                .unwrap_or(false),
            completion_kinds: text_document
                .and_then(|document| document.completion.as_ref())
                .and_then(|completion| completion.completion_item_kind.as_ref())
                .and_then(|kinds| kinds.value_set.clone()),
            symbol_kinds: document_symbol
                .and_then(|symbols| symbols.symbol_kind.as_ref())
                .and_then(|kinds| kinds.value_set.clone()),
        }
    }
}

pub struct Backend {
    client: Client,
    documents: RwLock<DocumentStore>,
    preferences: RwLock<ClientPreferences>,
    diagnostic_publication: Mutex<()>,
}

impl Backend {
    #[must_use]
    pub fn new(client: Client) -> Self {
        Self {
            client,
            documents: RwLock::new(DocumentStore::default()),
            preferences: RwLock::new(ClientPreferences::default()),
            diagnostic_publication: Mutex::new(()),
        }
    }

    async fn encoding(&self) -> PositionEncoding {
        self.preferences.read().await.encoding
    }

    async fn analyze_and_publish(
        &self,
        uri: Uri,
        expected_generation: u64,
        expected_revision: u64,
        expected_version: i32,
    ) {
        let Some(snapshot) = self.documents.read().await.snapshot(&uri) else {
            return;
        };
        if snapshot.generation != expected_generation
            || snapshot.revision != expected_revision
            || snapshot.version != expected_version
        {
            return;
        }
        let encoding = self.encoding().await;
        let analysis = if snapshot.oversized || snapshot.desynchronized {
            DocumentAnalysis {
                parsed: None,
                diagnostics: vec![server_diagnostic(
                    if snapshot.oversized {
                        "LSP001"
                    } else {
                        "LSP002"
                    },
                    if snapshot.oversized {
                        format!(
                            "document is too large for analysis (maximum {MAX_DOCUMENT_BYTES} bytes)"
                        )
                    } else {
                        ChangeError::ResyncRequired.to_string()
                    },
                )],
            }
        } else {
            let text = snapshot.text;
            let line_index = snapshot.line_index;
            match tokio::task::spawn_blocking(move || {
                analyze_document(&text, &line_index, encoding)
            })
            .await
            {
                Ok(analysis) => analysis,
                Err(_) => DocumentAnalysis {
                    parsed: None,
                    diagnostics: vec![server_diagnostic(
                        "LSP003",
                        "the analysis worker stopped unexpectedly".to_owned(),
                    )],
                },
            }
        };

        let DocumentAnalysis {
            parsed,
            diagnostics,
        } = analysis;
        let _publication = self.diagnostic_publication.lock().await;
        let installed = self.documents.write().await.install_analysis(
            &uri,
            expected_generation,
            expected_revision,
            expected_version,
            parsed,
        );
        if installed {
            let published_version = self
                .preferences
                .read()
                .await
                .diagnostics_version
                .then_some(expected_version);
            self.client
                .publish_diagnostics(uri, diagnostics, published_version)
                .await;
        }
    }

    async fn snapshot(&self, uri: &Uri) -> Option<DocumentSnapshot> {
        self.documents.read().await.snapshot(uri)
    }

    async fn diagnostics_version(&self, version: i32) -> Option<i32> {
        self.preferences
            .read()
            .await
            .diagnostics_version
            .then_some(version)
    }
}

fn server_diagnostic(code: &'static str, message: String) -> Diagnostic {
    Diagnostic::new(
        Range::default(),
        Some(DiagnosticSeverity::ERROR),
        Some(NumberOrString::String(code.to_owned())),
        Some("lokit-lsp".to_owned()),
        message,
        None,
        None,
    )
}

fn initialize_result(encoding: PositionEncoding) -> InitializeResult {
    InitializeResult {
        capabilities: ServerCapabilities {
            position_encoding: Some(encoding.as_lsp()),
            text_document_sync: Some(TextDocumentSyncCapability::Options(
                TextDocumentSyncOptions {
                    open_close: Some(true),
                    change: Some(TextDocumentSyncKind::INCREMENTAL),
                    ..TextDocumentSyncOptions::default()
                },
            )),
            hover_provider: Some(HoverProviderCapability::Simple(true)),
            completion_provider: Some(CompletionOptions {
                resolve_provider: Some(false),
                trigger_characters: Some(vec!["=".to_owned(), "\"".to_owned()]),
                ..CompletionOptions::default()
            }),
            document_symbol_provider: Some(OneOf::Left(true)),
            document_formatting_provider: Some(OneOf::Left(true)),
            folding_range_provider: Some(FoldingRangeProviderCapability::Simple(true)),
            ..ServerCapabilities::default()
        },
        server_info: Some(ServerInfo {
            name: "lokit-lsp".to_owned(),
            version: Some(env!("CARGO_PKG_VERSION").to_owned()),
        }),
        offset_encoding: None,
    }
}

impl LanguageServer for Backend {
    async fn initialize(&self, params: InitializeParams) -> Result<InitializeResult> {
        let preferences = ClientPreferences::negotiate(&params);
        let encoding = preferences.encoding;
        *self.preferences.write().await = preferences;
        Ok(initialize_result(encoding))
    }

    async fn initialized(&self, _: InitializedParams) {}

    async fn shutdown(&self) -> Result<()> {
        Ok(())
    }

    async fn did_open(&self, params: DidOpenTextDocumentParams) {
        let item = params.text_document;
        let uri = item.uri;
        let version = item.version;
        let publication = self.diagnostic_publication.lock().await;
        let generation = self
            .documents
            .write()
            .await
            .open(uri.clone(), item.text, version);
        drop(publication);
        self.analyze_and_publish(uri, generation, 0, version).await;
    }

    async fn did_change(&self, params: DidChangeTextDocumentParams) {
        let uri = params.text_document.uri;
        let version = params.text_document.version;
        let encoding = self.encoding().await;
        let publication = self.diagnostic_publication.lock().await;
        let outcome =
            self.documents
                .write()
                .await
                .change(&uri, &params.content_changes, version, encoding);
        match outcome {
            Ok(Some(snapshot)) => {
                drop(publication);
                self.analyze_and_publish(uri, snapshot.generation, snapshot.revision, version)
                    .await;
            }
            Ok(None) | Err(ChangeError::StaleVersion { .. }) => {
                drop(publication);
            }
            Err(error) => {
                let diagnostic_version = self.diagnostics_version(version).await;
                self.client
                    .publish_diagnostics(
                        uri,
                        vec![server_diagnostic("LSP002", error.to_string())],
                        diagnostic_version,
                    )
                    .await;
            }
        }
    }

    async fn did_close(&self, params: DidCloseTextDocumentParams) {
        let uri = params.text_document.uri;
        let _publication = self.diagnostic_publication.lock().await;
        self.documents.write().await.close(&uri);
        self.client.publish_diagnostics(uri, Vec::new(), None).await;
    }

    async fn completion(&self, params: CompletionParams) -> Result<Option<CompletionResponse>> {
        let uri = &params.text_document_position.text_document.uri;
        let Some(snapshot) = self.snapshot(uri).await else {
            return Ok(None);
        };
        if snapshot.desynchronized || snapshot.oversized {
            return Ok(None);
        }
        let position = params.text_document_position.position;
        let preferences = self.preferences.read().await.clone();
        let structure = snapshot.parsed.as_ref().map(|parsed| &parsed.document);
        Ok(Some(CompletionResponse::List(completions(
            &snapshot.text,
            &snapshot.line_index,
            position,
            preferences.encoding,
            structure,
            preferences.completion_kinds.as_deref(),
        ))))
    }

    async fn hover(&self, params: HoverParams) -> Result<Option<Hover>> {
        let uri = &params.text_document_position_params.text_document.uri;
        let Some(snapshot) = self.snapshot(uri).await else {
            return Ok(None);
        };
        if snapshot.desynchronized || snapshot.oversized {
            return Ok(None);
        }
        let preferences = self.preferences.read().await.clone();
        Ok(hover(
            &snapshot.text,
            &snapshot.line_index,
            params.text_document_position_params.position,
            preferences.encoding,
            preferences.hover_markup,
        ))
    }

    async fn document_symbol(
        &self,
        params: DocumentSymbolParams,
    ) -> Result<Option<DocumentSymbolResponse>> {
        let Some(snapshot) = self.snapshot(&params.text_document.uri).await else {
            return Ok(None);
        };
        if snapshot.parsed.is_none() || snapshot.desynchronized || snapshot.oversized {
            return Ok(None);
        }
        let preferences = self.preferences.read().await.clone();
        Ok(Some(document_symbols(
            &snapshot.text,
            &snapshot.line_index,
            preferences.encoding,
            &params.text_document.uri,
            preferences.hierarchical_symbols,
            preferences.symbol_kinds.as_deref(),
        )))
    }

    async fn folding_range(
        &self,
        params: FoldingRangeParams,
    ) -> Result<Option<Vec<tower_lsp_server::ls_types::FoldingRange>>> {
        let Some(snapshot) = self.snapshot(&params.text_document.uri).await else {
            return Ok(None);
        };
        if snapshot.desynchronized || snapshot.oversized {
            return Ok(None);
        }
        let preferences = self.preferences.read().await.clone();
        let maximum_ranges = preferences
            .folding_range_limit
            .map_or(crate::analysis::MAX_FOLDING_RANGES, |limit| {
                usize::try_from(limit).unwrap_or(usize::MAX)
            });
        Ok(Some(folding_ranges(
            &snapshot.text,
            &snapshot.line_index,
            maximum_ranges,
            preferences.folding_collapsed_text,
        )))
    }

    async fn formatting(&self, params: DocumentFormattingParams) -> Result<Option<Vec<TextEdit>>> {
        let uri = &params.text_document.uri;
        let Some(snapshot) = self.snapshot(uri).await else {
            return Ok(None);
        };
        if snapshot.desynchronized || snapshot.oversized {
            return Ok(None);
        }
        let Some(parsed) = snapshot.parsed else {
            return Ok(None);
        };
        if contains_source_comments(&snapshot.text) {
            return Ok(None);
        }
        let formatted = tokio::task::spawn_blocking(move || canonical_text(&parsed))
            .await
            .map_err(|_| Error::internal_error())?
            .map_err(Error::invalid_params)?;
        if self.documents.read().await.identity(uri)
            != Some((snapshot.generation, snapshot.revision, snapshot.version))
        {
            return Err(Error::content_modified());
        }
        if formatted.as_str() == snapshot.text.as_ref() {
            return Ok(Some(Vec::new()));
        }
        Ok(Some(vec![TextEdit::new(
            snapshot
                .line_index
                .full_document_range(&snapshot.text, self.encoding().await),
            formatted,
        )]))
    }
}

#[cfg(test)]
mod tests {
    use std::error::Error as StdError;
    use std::io;
    use std::str::FromStr;
    use std::time::Duration;

    use futures::StreamExt;
    use serde_json::json;
    use tower::{Service, ServiceExt};
    use tower_lsp_server::LspService;
    use tower_lsp_server::jsonrpc::Request;
    use tower_lsp_server::ls_types::{
        Position, PositionEncodingKind, Range, TextDocumentContentChangeEvent,
    };

    use super::*;

    const VALID_DOCUMENT: &str = "@lokit 1\ndocument {\n  source_locale = \"en\"\n  target_locale = \"fr\"\n}\nunit \"hello\" {\n  source = \"Hello\"\n  target \"fr\" {\n    text = \"Bonjour\"\n  }\n}\n";

    #[test]
    fn document_store_lifecycle_preserves_versions_and_discards_stale_analysis()
    -> std::result::Result<(), Box<dyn StdError>> {
        let mut store = DocumentStore::default();
        let uri = Uri::from_str("file:///workspace/messages.lokit")?;
        let generation = store.open(uri.clone(), "abc".to_owned(), 1);
        assert_eq!(store.identity(&uri), Some((generation, 0, 1)));

        let changes = [TextDocumentContentChangeEvent {
            range: Some(Range::new(Position::new(0, 1), Position::new(0, 2))),
            range_length: None,
            text: "z".to_owned(),
        }];
        let outcome = store.change(&uri, &changes, 2, PositionEncoding::Utf16);
        assert!(outcome.is_ok());
        if let Ok(Some(snapshot)) = outcome {
            assert_eq!(snapshot.text.as_ref(), "azc");
            assert_eq!(snapshot.version, 2);
        }
        assert!(!store.install_analysis(&uri, generation, 0, 1, None));
        assert!(store.install_analysis(&uri, generation, 1, 2, None));
        assert!(store.close(&uri));
        assert!(store.snapshot(&uri).is_none());
        Ok(())
    }

    #[test]
    fn oversized_documents_require_and_accept_a_full_resynchronization()
    -> std::result::Result<(), Box<dyn StdError>> {
        let uri = Uri::from_str("file:///workspace/large.lokit")?;
        let mut store = DocumentStore::default();
        store.open(uri.clone(), "x".repeat(MAX_DOCUMENT_BYTES + 1), 1);
        let initial = store.snapshot(&uri);
        assert!(initial.is_some());
        if let Some(initial) = initial {
            assert!(initial.oversized);
            assert!(initial.text.is_empty());
        }
        let incremental = [TextDocumentContentChangeEvent {
            range: Some(Range::new(Position::new(0, 0), Position::new(0, 0))),
            range_length: None,
            text: "x".to_owned(),
        }];
        assert!(matches!(
            store.change(&uri, &incremental, 2, PositionEncoding::Utf16),
            Err(ChangeError::ResyncRequired)
        ));

        let full = [TextDocumentContentChangeEvent {
            range: None,
            range_length: None,
            text: "small again".to_owned(),
        }];
        let result = store.change(&uri, &full, 2, PositionEncoding::Utf16);
        assert!(result.is_ok());
        if let Ok(Some(snapshot)) = result {
            assert_eq!(snapshot.text.as_ref(), "small again");
            assert!(!snapshot.oversized);
        }
        Ok(())
    }

    #[test]
    fn rejected_incremental_changes_force_a_full_resynchronization()
    -> std::result::Result<(), Box<dyn StdError>> {
        let uri = Uri::from_str("file:///workspace/desynchronized.lokit")?;
        let mut store = DocumentStore::default();
        store.open(uri.clone(), "abc".to_owned(), 1);
        let invalid = [TextDocumentContentChangeEvent {
            range: Some(Range::new(Position::new(4, 0), Position::new(4, 1))),
            range_length: None,
            text: "x".to_owned(),
        }];
        assert!(matches!(
            store.change(&uri, &invalid, 2, PositionEncoding::Utf16),
            Err(ChangeError::InvalidRange(_))
        ));
        let snapshot = store.snapshot(&uri);
        assert!(snapshot.as_ref().is_some_and(|value| value.desynchronized));
        assert!(matches!(
            store.change(&uri, &invalid, 3, PositionEncoding::Utf16),
            Err(ChangeError::ResyncRequired)
        ));

        let full = [TextDocumentContentChangeEvent {
            range: None,
            range_length: None,
            text: "resynchronized".to_owned(),
        }];
        let recovered = store.change(&uri, &full, 3, PositionEncoding::Utf16)?;
        assert!(recovered.as_ref().is_some_and(|value| {
            !value.desynchronized && value.text.as_ref() == "resynchronized"
        }));
        Ok(())
    }

    #[test]
    fn close_and_reopen_rejects_analysis_from_the_previous_generation()
    -> std::result::Result<(), Box<dyn StdError>> {
        let uri = Uri::from_str("file:///workspace/reopened.lokit")?;
        let mut store = DocumentStore::default();
        let old_generation = store.open(uri.clone(), "old".to_owned(), 1);
        assert!(store.close(&uri));
        let new_generation = store.open(uri.clone(), "new".to_owned(), 1);
        assert_ne!(old_generation, new_generation);
        assert!(!store.install_analysis(&uri, old_generation, 0, 1, None));
        assert_eq!(store.identity(&uri), Some((new_generation, 0, 1)));
        assert_eq!(
            store.snapshot(&uri).map(|value| value.text),
            Some(Arc::from("new"))
        );
        Ok(())
    }

    #[tokio::test]
    async fn delayed_analysis_cannot_overtake_a_newer_publication()
    -> std::result::Result<(), Box<dyn StdError>> {
        let uri = Uri::from_str("file:///workspace/publication-order.lokit")?;
        let store = Arc::new(RwLock::new(DocumentStore::default()));
        let generation = store.write().await.open(uri.clone(), "abc".to_owned(), 1);
        let publication = Arc::new(Mutex::new(()));
        let release_old = Arc::new(tokio::sync::Notify::new());
        let published = Arc::new(Mutex::new(Vec::new()));

        let old_store = Arc::clone(&store);
        let old_publication = Arc::clone(&publication);
        let old_release = Arc::clone(&release_old);
        let old_published = Arc::clone(&published);
        let old_uri = uri.clone();
        let old = tokio::spawn(async move {
            old_release.notified().await;
            let _guard = old_publication.lock().await;
            if old_store
                .write()
                .await
                .install_analysis(&old_uri, generation, 0, 1, None)
            {
                old_published.lock().await.push("old");
            }
        });

        {
            let _guard = publication.lock().await;
            let change = [TextDocumentContentChangeEvent {
                range: Some(Range::new(Position::new(0, 1), Position::new(0, 2))),
                range_length: None,
                text: "z".to_owned(),
            }];
            let snapshot = store
                .write()
                .await
                .change(&uri, &change, 2, PositionEncoding::Utf16)?;
            let snapshot =
                snapshot.ok_or_else(|| io::Error::other("document disappeared during test"))?;
            assert!(store.write().await.install_analysis(
                &uri,
                snapshot.generation,
                snapshot.revision,
                snapshot.version,
                None,
            ));
            published.lock().await.push("new");
        }
        release_old.notify_one();
        old.await?;
        assert_eq!(*published.lock().await, vec!["new"]);
        Ok(())
    }

    #[test]
    fn advertises_only_implemented_stable_capabilities() {
        let initialized = initialize_result(PositionEncoding::Utf8);
        assert_eq!(
            initialized.capabilities.position_encoding,
            Some(PositionEncodingKind::UTF8)
        );
        assert!(initialized.capabilities.completion_provider.is_some());
        assert!(initialized.capabilities.hover_provider.is_some());
        assert!(initialized.capabilities.document_symbol_provider.is_some());
        assert!(
            initialized
                .capabilities
                .document_formatting_provider
                .is_some()
        );
        assert!(initialized.capabilities.folding_range_provider.is_some());
        assert!(initialized.capabilities.diagnostic_provider.is_none());
        assert!(
            initialized
                .capabilities
                .inline_completion_provider
                .is_none()
        );
    }

    #[test]
    fn negotiates_supported_position_encodings() {
        assert_eq!(PositionEncoding::select(None), PositionEncoding::Utf16);
        assert_eq!(
            PositionEncoding::select(Some(&[PositionEncodingKind::UTF32])),
            PositionEncoding::Utf32
        );
        assert_eq!(
            PositionEncoding::select(Some(&[
                PositionEncodingKind::UTF16,
                PositionEncodingKind::UTF8,
            ])),
            PositionEncoding::Utf16
        );
    }

    #[test]
    fn negotiates_client_response_preferences() -> std::result::Result<(), Box<dyn StdError>> {
        let empty: InitializeParams = serde_json::from_value(json!({"capabilities": {}}))?;
        let empty = ClientPreferences::negotiate(&empty);
        assert!(!empty.hierarchical_symbols);
        assert_eq!(empty.hover_markup, MarkupKind::PlainText);
        assert!(!empty.folding_collapsed_text);
        assert_eq!(empty.folding_range_limit, None);
        assert!(!empty.diagnostics_version);
        assert!(empty.completion_kinds.is_none());
        assert!(empty.symbol_kinds.is_none());

        let rich: InitializeParams = serde_json::from_value(json!({
            "capabilities": {
                "textDocument": {
                    "completion": {
                        "completionItemKind": {"valueSet": [20, 22]}
                    },
                    "hover": {"contentFormat": ["markdown", "plaintext"]},
                    "documentSymbol": {
                        "hierarchicalDocumentSymbolSupport": true,
                        "symbolKind": {"valueSet": [19, 23]}
                    },
                    "publishDiagnostics": {"versionSupport": true},
                    "foldingRange": {
                        "rangeLimit": 2,
                        "foldingRange": {"collapsedText": true}
                    }
                }
            }
        }))?;
        let rich = ClientPreferences::negotiate(&rich);
        assert!(rich.hierarchical_symbols);
        assert_eq!(rich.hover_markup, MarkupKind::Markdown);
        assert!(rich.folding_collapsed_text);
        assert_eq!(rich.folding_range_limit, Some(2));
        assert!(rich.diagnostics_version);
        assert_eq!(
            rich.completion_kinds,
            Some(vec![
                CompletionItemKind::ENUM_MEMBER,
                CompletionItemKind::STRUCT
            ])
        );
        assert_eq!(
            rich.symbol_kinds,
            Some(vec![SymbolKind::OBJECT, SymbolKind::STRUCT])
        );
        Ok(())
    }

    #[tokio::test]
    async fn protocol_publishes_versioned_diagnostics_and_clears_them()
    -> std::result::Result<(), Box<dyn StdError>> {
        let (mut service, mut socket) = LspService::new(Backend::new);
        let initialize = Request::build("initialize")
            .params(json!({
                "capabilities": {
                    "textDocument": {
                        "publishDiagnostics": {"versionSupport": true}
                    }
                }
            }))
            .id(1)
            .finish();
        service.ready().await?.call(initialize).await?;
        service
            .ready()
            .await?
            .call(Request::build("initialized").params(json!({})).finish())
            .await?;

        let open = Request::build("textDocument/didOpen")
            .params(json!({
                "textDocument": {
                    "uri": "file:///workspace/invalid.lokit",
                    "languageId": "lokit",
                    "version": 7,
                    "text": "not a lokit file\n"
                }
            }))
            .finish();
        service.ready().await?.call(open).await?;

        let published = tokio::time::timeout(Duration::from_secs(2), socket.next())
            .await?
            .ok_or_else(|| io::Error::other("diagnostic notification channel closed"))?;
        let published = serde_json::to_value(published)?;
        assert_eq!(published["method"], "textDocument/publishDiagnostics");
        assert_eq!(published["params"]["version"], 7);
        assert_eq!(published["params"]["diagnostics"][0]["code"], "LKT005");

        let change = Request::build("textDocument/didChange")
            .params(json!({
                "textDocument": {
                    "uri": "file:///workspace/invalid.lokit",
                    "version": 8
                },
                "contentChanges": [{
                    "range": {
                        "start": {"line": 0, "character": 0},
                        "end": {"line": 1, "character": 0}
                    },
                    "text": "@lokit 1\ndocument {\n  source_locale = \"en\"\n}\n"
                }]
            }))
            .finish();
        service.ready().await?.call(change).await?;
        let valid = tokio::time::timeout(Duration::from_secs(2), socket.next())
            .await?
            .ok_or_else(|| io::Error::other("valid diagnostic notification channel closed"))?;
        let valid = serde_json::to_value(valid)?;
        assert_eq!(valid["params"]["version"], 8);
        assert_eq!(valid["params"]["diagnostics"], json!([]));

        let close = Request::build("textDocument/didClose")
            .params(json!({
                "textDocument": {"uri": "file:///workspace/invalid.lokit"}
            }))
            .finish();
        service.ready().await?.call(close).await?;
        let cleared = tokio::time::timeout(Duration::from_secs(2), socket.next())
            .await?
            .ok_or_else(|| io::Error::other("diagnostic clear channel closed"))?;
        let cleared = serde_json::to_value(cleared)?;
        assert_eq!(cleared["method"], "textDocument/publishDiagnostics");
        assert_eq!(cleared["params"]["diagnostics"], json!([]));
        assert!(cleared["params"]["version"].is_null());
        Ok(())
    }

    #[tokio::test]
    #[allow(clippy::too_many_lines)]
    async fn protocol_negotiates_rich_capability_response_shapes()
    -> std::result::Result<(), Box<dyn StdError>> {
        let (mut service, mut socket) = LspService::new(Backend::new);
        let initialize = Request::build("initialize")
            .params(json!({
                "capabilities": {
                    "textDocument": {
                        "completion": {
                            "completionItemKind": {"valueSet": [20, 22]}
                        },
                        "hover": {"contentFormat": ["markdown"]},
                        "documentSymbol": {
                            "hierarchicalDocumentSymbolSupport": true,
                            "symbolKind": {"valueSet": [19, 23]}
                        },
                        "publishDiagnostics": {"versionSupport": true},
                        "foldingRange": {
                            "rangeLimit": 2,
                            "foldingRange": {"collapsedText": true}
                        }
                    }
                }
            }))
            .id(1)
            .finish();
        service.ready().await?.call(initialize).await?;
        service
            .ready()
            .await?
            .call(Request::build("initialized").params(json!({})).finish())
            .await?;
        service
            .ready()
            .await?
            .call(
                Request::build("textDocument/didOpen")
                    .params(json!({
                        "textDocument": {
                            "uri": "file:///workspace/rich.lokit",
                            "languageId": "lokit",
                            "version": 1,
                            "text": VALID_DOCUMENT
                        }
                    }))
                    .finish(),
            )
            .await?;
        let diagnostics = tokio::time::timeout(Duration::from_secs(2), socket.next())
            .await?
            .ok_or_else(|| io::Error::other("diagnostic channel closed"))?;
        let diagnostics = serde_json::to_value(diagnostics)?;
        assert_eq!(diagnostics["params"]["version"], 1);

        let symbols = service
            .ready()
            .await?
            .call(
                Request::build("textDocument/documentSymbol")
                    .params(json!({
                        "textDocument": {"uri": "file:///workspace/rich.lokit"}
                    }))
                    .id(2)
                    .finish(),
            )
            .await?;
        let symbols = serde_json::to_value(symbols)?;
        assert!(symbols["result"][1]["children"].is_array());
        assert_eq!(symbols["result"][1]["kind"], 19);

        let hover = service
            .ready()
            .await?
            .call(
                Request::build("textDocument/hover")
                    .params(json!({
                        "textDocument": {"uri": "file:///workspace/rich.lokit"},
                        "position": {"line": 2, "character": 5}
                    }))
                    .id(3)
                    .finish(),
            )
            .await?;
        let hover = serde_json::to_value(hover)?;
        assert_eq!(hover["result"]["contents"]["kind"], "markdown");

        let folding = service
            .ready()
            .await?
            .call(
                Request::build("textDocument/foldingRange")
                    .params(json!({
                        "textDocument": {"uri": "file:///workspace/rich.lokit"}
                    }))
                    .id(4)
                    .finish(),
            )
            .await?;
        let folding = serde_json::to_value(folding)?;
        assert_eq!(folding["result"].as_array().map(Vec::len), Some(2));
        assert!(folding["result"][0]["collapsedText"].is_string());

        let completion = service
            .ready()
            .await?
            .call(
                Request::build("textDocument/completion")
                    .params(json!({
                        "textDocument": {"uri": "file:///workspace/rich.lokit"},
                        "position": {"line": 6, "character": 2}
                    }))
                    .id(5)
                    .finish(),
            )
            .await?;
        let completion = serde_json::to_value(completion)?;
        let items = completion["result"]["items"]
            .as_array()
            .ok_or_else(|| io::Error::other("completion response did not contain a list"))?;
        let plural = items
            .iter()
            .find(|item| item["label"] == "plural")
            .ok_or_else(|| io::Error::other("plural completion missing"))?;
        assert_eq!(plural["kind"], 22);
        Ok(())
    }

    #[tokio::test]
    #[allow(clippy::too_many_lines)]
    async fn protocol_uses_legacy_safe_shapes_for_empty_capabilities()
    -> std::result::Result<(), Box<dyn StdError>> {
        let (mut service, mut socket) = LspService::new(Backend::new);
        service
            .ready()
            .await?
            .call(
                Request::build("initialize")
                    .params(json!({"capabilities": {}}))
                    .id(1)
                    .finish(),
            )
            .await?;
        service
            .ready()
            .await?
            .call(Request::build("initialized").params(json!({})).finish())
            .await?;
        service
            .ready()
            .await?
            .call(
                Request::build("textDocument/didOpen")
                    .params(json!({
                        "textDocument": {
                            "uri": "file:///workspace/legacy.lokit",
                            "languageId": "lokit",
                            "version": 1,
                            "text": VALID_DOCUMENT
                        }
                    }))
                    .finish(),
            )
            .await?;
        let diagnostics = tokio::time::timeout(Duration::from_secs(2), socket.next())
            .await?
            .ok_or_else(|| io::Error::other("diagnostic channel closed"))?;
        let diagnostics = serde_json::to_value(diagnostics)?;
        assert!(diagnostics["params"]["version"].is_null());

        let symbols = service
            .ready()
            .await?
            .call(
                Request::build("textDocument/documentSymbol")
                    .params(json!({
                        "textDocument": {"uri": "file:///workspace/legacy.lokit"}
                    }))
                    .id(2)
                    .finish(),
            )
            .await?;
        let symbols = serde_json::to_value(symbols)?;
        assert!(symbols["result"][0]["location"].is_object());
        assert!(symbols["result"][0]["selectionRange"].is_null());
        assert_eq!(symbols["result"][1]["kind"], 5);

        let hover = service
            .ready()
            .await?
            .call(
                Request::build("textDocument/hover")
                    .params(json!({
                        "textDocument": {"uri": "file:///workspace/legacy.lokit"},
                        "position": {"line": 2, "character": 5}
                    }))
                    .id(3)
                    .finish(),
            )
            .await?;
        let hover = serde_json::to_value(hover)?;
        assert_eq!(hover["result"]["contents"]["kind"], "plaintext");

        let folding = service
            .ready()
            .await?
            .call(
                Request::build("textDocument/foldingRange")
                    .params(json!({
                        "textDocument": {"uri": "file:///workspace/legacy.lokit"}
                    }))
                    .id(4)
                    .finish(),
            )
            .await?;
        let folding = serde_json::to_value(folding)?;
        assert!(folding["result"][0]["collapsedText"].is_null());

        let completion = service
            .ready()
            .await?
            .call(
                Request::build("textDocument/completion")
                    .params(json!({
                        "textDocument": {"uri": "file:///workspace/legacy.lokit"},
                        "position": {"line": 6, "character": 2}
                    }))
                    .id(5)
                    .finish(),
            )
            .await?;
        let completion = serde_json::to_value(completion)?;
        let items = completion["result"]["items"]
            .as_array()
            .ok_or_else(|| io::Error::other("completion response did not contain a list"))?;
        let plural = items
            .iter()
            .find(|item| item["label"] == "plural")
            .ok_or_else(|| io::Error::other("plural completion missing"))?;
        assert_eq!(plural["kind"], 7);
        Ok(())
    }

    #[tokio::test]
    #[allow(clippy::too_many_lines)]
    async fn protocol_completes_incomplete_edits_and_recomputes_truncated_lists()
    -> std::result::Result<(), Box<dyn StdError>> {
        let mut locales: Vec<String> = (0..160).map(|index| format!("locale-{index:03}")).collect();
        locales.push("zz-last".to_owned());
        let encoded_locales = serde_json::to_string(&locales)?;
        let valid = format!(
            "@lokit 1\ndocument {{\n  source_locale = \"en\"\n  target_locales = {encoded_locales}\n}}\nunit \"u\" {{\n  source = \"s\"\n  target \"locale-159\" {{\n    text = \"t\"\n  }}\n}}\n"
        );
        let (mut service, mut socket) = LspService::new(Backend::new);
        service
            .ready()
            .await?
            .call(
                Request::build("initialize")
                    .params(json!({"capabilities": {}}))
                    .id(1)
                    .finish(),
            )
            .await?;
        service
            .ready()
            .await?
            .call(Request::build("initialized").params(json!({})).finish())
            .await?;
        service
            .ready()
            .await?
            .call(
                Request::build("textDocument/didOpen")
                    .params(json!({
                        "textDocument": {
                            "uri": "file:///workspace/completion.lokit",
                            "languageId": "lokit",
                            "version": 1,
                            "text": valid
                        }
                    }))
                    .finish(),
            )
            .await?;
        tokio::time::timeout(Duration::from_secs(2), socket.next())
            .await?
            .ok_or_else(|| io::Error::other("open diagnostic channel closed"))?;

        service
            .ready()
            .await?
            .call(
                Request::build("textDocument/didChange")
                    .params(json!({
                        "textDocument": {
                            "uri": "file:///workspace/completion.lokit",
                            "version": 2
                        },
                        "contentChanges": [{
                            "range": {
                                "start": {"line": 7, "character": 9},
                                "end": {"line": 7, "character": 23}
                            },
                            "text": ""
                        }]
                    }))
                    .finish(),
            )
            .await?;
        tokio::time::timeout(Duration::from_secs(2), socket.next())
            .await?
            .ok_or_else(|| io::Error::other("invalid diagnostic channel closed"))?;

        let all = service
            .ready()
            .await?
            .call(
                Request::build("textDocument/completion")
                    .params(json!({
                        "textDocument": {"uri": "file:///workspace/completion.lokit"},
                        "position": {"line": 7, "character": 9}
                    }))
                    .id(2)
                    .finish(),
            )
            .await?;
        let all = serde_json::to_value(all)?;
        assert_eq!(all["result"]["isIncomplete"], true);
        assert_eq!(
            all["result"]["items"].as_array().map(Vec::len),
            Some(crate::analysis::MAX_COMPLETIONS)
        );

        service
            .ready()
            .await?
            .call(
                Request::build("textDocument/didChange")
                    .params(json!({
                        "textDocument": {
                            "uri": "file:///workspace/completion.lokit",
                            "version": 3
                        },
                        "contentChanges": [{
                            "range": {
                                "start": {"line": 7, "character": 9},
                                "end": {"line": 7, "character": 9}
                            },
                            "text": "zz"
                        }]
                    }))
                    .finish(),
            )
            .await?;
        tokio::time::timeout(Duration::from_secs(2), socket.next())
            .await?
            .ok_or_else(|| io::Error::other("filtered diagnostic channel closed"))?;
        let filtered = service
            .ready()
            .await?
            .call(
                Request::build("textDocument/completion")
                    .params(json!({
                        "textDocument": {"uri": "file:///workspace/completion.lokit"},
                        "position": {"line": 7, "character": 11}
                    }))
                    .id(3)
                    .finish(),
            )
            .await?;
        let filtered = serde_json::to_value(filtered)?;
        assert_eq!(filtered["result"]["isIncomplete"], false);
        assert_eq!(filtered["result"]["items"][0]["label"], "zz-last");
        assert_eq!(
            filtered["result"]["items"][0]["textEdit"]["range"]["start"]["character"],
            9
        );

        let code_document = "@lokit 1\ndocument {\n  source_locale = \"en\"\n}\nunit \"u\" {\n  source = \"s\"\n  tags {\n    source_tag \"open\" {\n      id = \"b1\"\n      type = strong.open\n    }\n    source_parts {\n      code = \"open\"\n    }\n  }\n}\n";
        service
            .ready()
            .await?
            .call(
                Request::build("textDocument/didChange")
                    .params(json!({
                        "textDocument": {
                            "uri": "file:///workspace/completion.lokit",
                            "version": 4
                        },
                        "contentChanges": [{"text": code_document}]
                    }))
                    .finish(),
            )
            .await?;
        tokio::time::timeout(Duration::from_secs(2), socket.next())
            .await?
            .ok_or_else(|| io::Error::other("code resync diagnostic channel closed"))?;
        service
            .ready()
            .await?
            .call(
                Request::build("textDocument/didChange")
                    .params(json!({
                        "textDocument": {
                            "uri": "file:///workspace/completion.lokit",
                            "version": 5
                        },
                        "contentChanges": [{
                            "range": {
                                "start": {"line": 12, "character": 13},
                                "end": {"line": 12, "character": 19}
                            },
                            "text": ""
                        }]
                    }))
                    .finish(),
            )
            .await?;
        tokio::time::timeout(Duration::from_secs(2), socket.next())
            .await?
            .ok_or_else(|| io::Error::other("code edit diagnostic channel closed"))?;
        let code = service
            .ready()
            .await?
            .call(
                Request::build("textDocument/completion")
                    .params(json!({
                        "textDocument": {"uri": "file:///workspace/completion.lokit"},
                        "position": {"line": 12, "character": 13}
                    }))
                    .id(4)
                    .finish(),
            )
            .await?;
        let code = serde_json::to_value(code)?;
        assert_eq!(code["result"]["items"][0]["label"], "open");
        assert_eq!(
            code["result"]["items"][0]["textEdit"]["newText"],
            "\"open\""
        );
        Ok(())
    }

    #[tokio::test]
    async fn protocol_rejects_ranges_until_a_full_resynchronization()
    -> std::result::Result<(), Box<dyn StdError>> {
        let (mut service, mut socket) = LspService::new(Backend::new);
        service
            .ready()
            .await?
            .call(
                Request::build("initialize")
                    .params(json!({"capabilities": {}}))
                    .id(1)
                    .finish(),
            )
            .await?;
        service
            .ready()
            .await?
            .call(Request::build("initialized").params(json!({})).finish())
            .await?;
        service
            .ready()
            .await?
            .call(
                Request::build("textDocument/didOpen")
                    .params(json!({
                        "textDocument": {
                            "uri": "file:///workspace/resync.lokit",
                            "languageId": "lokit",
                            "version": 1,
                            "text": VALID_DOCUMENT
                        }
                    }))
                    .finish(),
            )
            .await?;
        tokio::time::timeout(Duration::from_secs(2), socket.next())
            .await?
            .ok_or_else(|| io::Error::other("open diagnostic channel closed"))?;

        for (version, line) in [(2, 500), (3, 0)] {
            service
                .ready()
                .await?
                .call(
                    Request::build("textDocument/didChange")
                        .params(json!({
                            "textDocument": {
                                "uri": "file:///workspace/resync.lokit",
                                "version": version
                            },
                            "contentChanges": [{
                                "range": {
                                    "start": {"line": line, "character": 0},
                                    "end": {"line": line, "character": 0}
                                },
                                "text": "x"
                            }]
                        }))
                        .finish(),
                )
                .await?;
            let rejected = tokio::time::timeout(Duration::from_secs(2), socket.next())
                .await?
                .ok_or_else(|| io::Error::other("rejection diagnostic channel closed"))?;
            let rejected = serde_json::to_value(rejected)?;
            assert_eq!(rejected["params"]["diagnostics"][0]["code"], "LSP002");
            if version == 3 {
                assert!(
                    rejected["params"]["diagnostics"][0]["message"]
                        .as_str()
                        .is_some_and(|message| message.contains("out of sync"))
                );
            }
        }

        service
            .ready()
            .await?
            .call(
                Request::build("textDocument/didChange")
                    .params(json!({
                        "textDocument": {
                            "uri": "file:///workspace/resync.lokit",
                            "version": 3
                        },
                        "contentChanges": [{"text": VALID_DOCUMENT}]
                    }))
                    .finish(),
            )
            .await?;
        let recovered = tokio::time::timeout(Duration::from_secs(2), socket.next())
            .await?
            .ok_or_else(|| io::Error::other("recovery diagnostic channel closed"))?;
        let recovered = serde_json::to_value(recovered)?;
        assert_eq!(recovered["params"]["diagnostics"], json!([]));
        Ok(())
    }

    #[tokio::test]
    async fn protocol_publishes_an_oversized_open_diagnostic()
    -> std::result::Result<(), Box<dyn StdError>> {
        let (mut service, mut socket) = LspService::new(Backend::new);
        service
            .ready()
            .await?
            .call(
                Request::build("initialize")
                    .params(json!({"capabilities": {}}))
                    .id(1)
                    .finish(),
            )
            .await?;
        service
            .ready()
            .await?
            .call(Request::build("initialized").params(json!({})).finish())
            .await?;
        service
            .ready()
            .await?
            .call(
                Request::build("textDocument/didOpen")
                    .params(json!({
                        "textDocument": {
                            "uri": "file:///workspace/oversized.lokit",
                            "languageId": "lokit",
                            "version": 1,
                            "text": "x".repeat(MAX_DOCUMENT_BYTES + 1)
                        }
                    }))
                    .finish(),
            )
            .await?;
        let diagnostic = tokio::time::timeout(Duration::from_secs(2), socket.next())
            .await?
            .ok_or_else(|| io::Error::other("oversized diagnostic channel closed"))?;
        let diagnostic = serde_json::to_value(diagnostic)?;
        assert_eq!(diagnostic["params"]["diagnostics"][0]["code"], "LSP001");
        assert!(diagnostic["params"]["version"].is_null());
        Ok(())
    }
}
