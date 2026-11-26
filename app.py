"""Conventional Flask entrypoint.

`wsgi:app` is used by the Docker/Gunicorn command.  This module lets platforms
or developers run `python app.py` or `flask --app app run` directly.
"""

import os

from meal_subsidy import create_app

app = create_app()

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        threaded=True,
        debug=False,
    )
