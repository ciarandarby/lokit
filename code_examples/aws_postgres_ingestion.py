from config import Config as config

from lokit.db import TranslationMemory, connect_sync
from lokit.parsers import stream


class DatabaseExample:
    def __init__(
        self,
        ingest_file_path: str = config.ingest_file_dir,
        source_locale: str = "en-IE",
        target_locale: str = "fr-FR",
    ) -> None:
        self.db_uri = (
            f"postgresql://{config.user}@{config.host}:{config.port}/{config.dbname}"
        )
        self.ingest_file = ingest_file_path
        self.source_locale = source_locale
        self.target_locale = target_locale
        self.tm_database: TranslationMemory = connect_sync(
            self.db_uri,
            password=config.auth_token,
            ssl=True,
        )

    def setup(self) -> None:
        self.tm_database.setup_sync(partitioned=True)

    def ingest(self, batch_size: int = 5000) -> None:
        stats = self.tm_database.load_sync(
            stream.tmx(self.ingest_file),
            batch_size=batch_size,
        )
        print(f"Loaded {stats.units_written} units in {stats.seconds:.2f} seconds.")

    def get_tm_matches(
        self,
        query_string: str,
        *,
        limit: int = 5,
        threshold: float = 0.5,
    ) -> None:
        matches = self.tm_database.match_sync(
            source=query_string,
            source_locale=self.source_locale,
            target_locale=self.target_locale,
            limit=limit,
            threshold=threshold,
        )
        print(f"Matches: {len(matches)}")
        for index, match in enumerate(matches, 1):
            unit = self.tm_database.unit_sync(
                match.unit_id,
                source_locale=self.source_locale,
                target_locale=self.target_locale,
            )
            print(
                f"{index} | target: {unit.target or ''} | "
                f"score: {match.score:.2f} | type: {match.kind}"
            )

    def close(self) -> None:
        self.tm_database.close_sync()

    def run_search_loop(self) -> None:
        try:
            while True:
                query = input("Search TM:\n").strip()
                if query:
                    self.get_tm_matches(query)
        except KeyboardInterrupt:
            print("\nExiting")
        finally:
            self.close()


if __name__ == "__main__":
    database = DatabaseExample()
    database.setup()
    database.ingest()
    database.run_search_loop()
