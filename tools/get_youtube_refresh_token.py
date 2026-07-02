from __future__ import annotations

import os

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ("https://www.googleapis.com/auth/youtube.force-ssl",)


def main() -> None:
    load_dotenv()
    client_id = os.getenv("YOUTUBE_CLIENT_ID")
    client_secret = os.getenv("YOUTUBE_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise SystemExit("Preencha YOUTUBE_CLIENT_ID e YOUTUBE_CLIENT_SECRET no .env.")

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    credentials = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
    )
    print("YOUTUBE_REFRESH_TOKEN=" + str(credentials.refresh_token))


if __name__ == "__main__":
    main()
