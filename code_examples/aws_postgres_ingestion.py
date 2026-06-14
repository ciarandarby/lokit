import os

import lokit

DATABASE_URI = os.environ.get(
    "LOKIT_DATABASE_URI",
    "postgresql://user:password@localhost:5432/translation_memory",
)
DATABASE_PASSWORD = os.environ.get("LOKIT_DATABASE_PASSWORD")


class DatabaseExample:
    def __init__(
        self,
        ingest_file_path: str = "translation_memory.tmx",
        source_locale: str = "en-US",
        target_locale: str = "fr-FR",
    ) -> None:
        self.ingest_file_path = ingest_file_path
        self.source_locale = source_locale
        self.target_locale = target_locale
        self.tm_database = lokit.database.connect_sync(
            DATABASE_URI,
            password=DATABASE_PASSWORD,
            ssl=True,
        )

    def setup(self) -> None:
        self.tm_database.setup_sync(partitioned=True)

    def ingest(self, batch_size: int = 5000) -> None:
        document = lokit.stream.tmx(self.ingest_file_path)
        stats = self.tm_database.load_sync(document, batch_size=batch_size)
        print(f"Loaded {stats.units_written} units in {stats.seconds:.2f} seconds.")

    def get_tm_matches(
        self,
        source_text: str,
        *,
        limit: int = 5,
        threshold: float = 0.5,
    ) -> None:
        matches = self.tm_database.match_sync(
            source=source_text,
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
            print(f"{index} | target: {unit.target or ''} | score: {match.score:.2f} | type: {match.kind}")

    def close(self) -> None:
        self.tm_database.close_sync()

    def run_search_loop(self) -> None:
        try:
            while True:
                source_text = input("Search translation memory:\n").strip()
                if source_text:
                    self.get_tm_matches(source_text)
        except KeyboardInterrupt:
            print("\nExiting")
        finally:
            self.close()


if __name__ == "__main__":
    database = DatabaseExample()
    database.setup()
    database.ingest()
    database.run_search_loop()
