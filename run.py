"""Spuštění vývojového serveru: python run.py  ->  http://127.0.0.1:5000"""
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

# C10: čas u každého záznamu (i u startu serveru a přístupového logu Werkzeug), výstup dál na stderr.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from charging import create_app  # noqa: E402
from charging.db import SCHEMA_VERSION  # noqa: E402

app = create_app()

if __name__ == "__main__":
    # C10: z logu má být poznat, kdy aplikace naběhla a nad jakou DB (jen název souboru, cesta obsahuje jméno uživatele).
    app.logger.info("VoltBook start: databáze %s, schéma v%s", Path(app.config["DATABASE"]).name, SCHEMA_VERSION)
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=os.environ.get("FLASK_DEBUG") == "1")
