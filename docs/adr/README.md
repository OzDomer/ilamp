# Architecture decision records

One short file per decision that had real alternatives. Written when the decision is made, not after. A record is never edited to say something else; if a decision changes, a new record supersedes it.

Format: context, decision, alternatives considered, consequences.

| # | Decision |
|---|---|
| [0001](0001-server-owns-the-lamp.md) | One server process is the only thing that talks to the lamp |
| [0002](0002-fastapi-over-aiohttp.md) | FastAPI for the hub |
