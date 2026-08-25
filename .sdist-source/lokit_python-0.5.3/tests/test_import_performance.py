from __future__ import annotations

import subprocess
import sys
import textwrap


def test_lokit_import_does_not_load_db_or_office_backend() -> None:
    code = textwrap.dedent(
        """
        import sys
        import lokit
        assert "lokit.db.connection" not in sys.modules
        assert "lokit.office.backend" not in sys.modules
        """
    )

    subprocess.run([sys.executable, "-c", code], check=True)


def test_lazy_module_access() -> None:
    code = textwrap.dedent(
        """
        import sys
        import lokit
        _ = lokit.parse
        assert "lokit.parse" in sys.modules
        assert "lokit.database" not in sys.modules
        """
    )

    subprocess.run([sys.executable, "-c", code], check=True)
