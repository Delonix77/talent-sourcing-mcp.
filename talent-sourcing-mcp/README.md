# Talent Sourcing MCP

An MCP server that finds candidates online, cross-checks them across independent
public sources, and gives each one a **verification score (0–100)** plus a
**role-fit score**. Any MCP-compatible tool can use it: Claude, Cursor, VS Code,
n8n, your own agents, or an ATS integration over HTTP.

It only uses official APIs and data people have made public. It does **not**
scrape LinkedIn or bypass any site's terms.

## What "verified" means

A candidate is scored on eight checks, each with the evidence shown:

| Check | Points | What it looks for |
|---|---|---|
| Independent sources | 20 | Found on 2+ sources (15) or 3+ (20) |
| Name consistency | 15 | Same name across sources |
| Location consistency | 10 | Same location across sources |
| Recent activity | 15 | Active within 90 days (15) or a year (8) |
| Account maturity | 10 | Accounts 2+ years old (10) or 1+ year (5): screens out fake profiles |
| Track record | 10 | GitHub followers/stars, Stack Overflow reputation, or years of experience |
| Contact verified | 15 | Email confirmed deliverable |
| Professional profile | 5 | LinkedIn profile linked |

**70+ = verified**, **40–69 = likely genuine**, **under 40 = unverified**.
"Verified" means the evidence is corroborated. It isn't proof of identity, so a
recruiter should still review it.

## Data sources

| Source | Key needed? | Good for |
|---|---|---|
| GitHub | Optional (`GITHUB_TOKEN`, free) | Software engineers; real code activity |
| Stack Overflow | Optional (`STACKEXCHANGE_KEY`, free) | Proven technical experts by technology |
| People Data Labs | `PDL_API_KEY` (free tier, then paid) | **Any role or industry**: sales, finance, HR, ops; LinkedIn URLs, years of experience |
| Hunter | `HUNTER_API_KEY` (free tier, then paid) | Finding and verifying work emails |

GitHub and Stack Overflow work with no keys at all. Add People Data Labs to source
non-tech roles, and Hunter to verify contact details.

## Tools

| Tool | What it does |
|---|---|
| `find_verified_talent` | **One-call pipeline**: searches all sources for a role, cross-verifies the best matches, ranks by 60% fit + 40% verification |
| `search_github_talent` | Engineers by location, language, followers, repos |
| `search_stackoverflow_experts` | Top answerers for a technology tag, filtered by location |
| `search_people` | Any job title / location / skills / experience (PDL) |
| `enrich_person` | Full profile from a LinkedIn URL, GitHub URL, email, or name + company (PDL) |
| `verify_candidate` | Cross-check one person across every source; returns score + evidence |
| `score_candidate_fit` | Fit score against skills, location, years of experience |
| `find_email` / `verify_email` | Find and check work emails (Hunter) |
| `shortlist_add` / `shortlist_view` / `shortlist_remove` | Named shortlists saved on disk |
| `shortlist_export` | CSV (for spreadsheets / ATS import) or JSON |
| `do_not_contact` | Blocks a person everywhere: removed from shortlists, hidden from searches |
| `list_sources` | Shows which sources are configured |

## Install

Requires Python 3.10+.

```bash
cd talent-sourcing-mcp
pip install .
# or, with uv:
uv tool install .
```

Copy `.env.example`, fill in any keys you have, and pass them as environment
variables in the configs below.

## Connect it to your tools

### Claude Desktop

Edit `claude_desktop_config.json` (Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "talent-sourcing": {
      "command": "talent-sourcing-mcp",
      "env": {
        "GITHUB_TOKEN": "ghp_...",
        "PDL_API_KEY": "...",
        "HUNTER_API_KEY": "..."
      }
    }
  }
}
```

### Claude Code

```bash
claude mcp add talent-sourcing -e GITHUB_TOKEN=ghp_... -e PDL_API_KEY=... -- talent-sourcing-mcp
```

### Cursor / VS Code / Windsurf

Use the same `command` + `env` block in the editor's MCP settings
(`.cursor/mcp.json`, `.vscode/mcp.json`, and so on).

### As a network service (n8n, Make, custom apps, a shared team server)

```bash
export TALENT_MCP_AUTH_TOKEN="$(openssl rand -hex 32)"
talent-sourcing-mcp --transport http --host 0.0.0.0 --port 8000
```

Clients connect to `http://your-server:8000/mcp` (MCP Streamable HTTP) and send
`Authorization: Bearer <token>`. The server refuses to listen on a public address
without a token. Put it behind HTTPS (e.g. Caddy, nginx, Cloudflare Tunnel) in
production.

#### Docker

```bash
docker build -t talent-sourcing-mcp .
docker run -p 8000:8000 -e TALENT_MCP_AUTH_TOKEN=... -e PDL_API_KEY=... \
  -v talent-data:/data talent-sourcing-mcp
```

## Example prompts once connected

- "Find verified Python/Django engineers in Lagos with 4+ years' experience and shortlist the top 5 as `moniepoint-backend`."
- "Find talent acquisition specialists in Nigeria with 5+ years, verify the top 10, and export a CSV."
- "Verify this candidate: github.com/someone, and find their work email at paystack.com."
- "Add jane@example.com to do-not-contact: she asked us to stop."

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `GITHUB_TOKEN` | – | Raises GitHub limit from 60 to 5,000 requests/hour |
| `STACKEXCHANGE_KEY` | – | Raises Stack Exchange limit from 300 to 10,000/day |
| `PDL_API_KEY` | – | Enables any-role search and enrichment |
| `HUNTER_API_KEY` | – | Enables email find/verify |
| `TALENT_MCP_DATA_DIR` | `~/.talent-sourcing-mcp` | Where shortlists, exports, and the do-not-contact list are saved |
| `TALENT_MCP_AUTH_TOKEN` | – | Bearer token required in HTTP mode |
| `TALENT_MCP_TIMEOUT` | `20` | API timeout in seconds |

**Cost control:** `find_verified_talent` cross-checks only the top `verify_top`
results (default 5), so paid API credits stay predictable.

## Responsible use

- Use only what people have made public, and only for recruiting.
- Honour opt-outs immediately with `do_not_contact`.
- Data protection law applies to you, not just to the data providers. In Nigeria
  that's the NDPA 2023; in the EU/UK, GDPR. Tell candidates where you found their
  details when you first contact them, and delete data you no longer need.
- Respect each provider's terms and rate limits.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The tests are fully offline (all APIs mocked).
