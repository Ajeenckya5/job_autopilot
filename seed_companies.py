#!/usr/bin/env python3
"""Seed watched-company list with FAANG, AI labs, quant/trading firms,
investment banks, and high-signal AI startups.

Run once:  python3 seed_companies.py
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path.home() / "Downloads/job-autopilot/autopilot.sqlite"

COMPANIES = [
    # ─── FAANG ────────────────────────────────────────────────────────────────
    ("amazon.com",          "Amazon",                   ""),
    ("microsoft.com",       "Microsoft",                "https://careers.microsoft.com"),

    # ─── AI LABS & FRONTIER MODEL COMPANIES ──────────────────────────────────
    ("openai.com",          "OpenAI",                   ""),
    ("anthropic.com",       "Anthropic",                "https://boards.greenhouse.io/anthropic"),
    ("mistral.ai",          "Mistral AI",               "https://jobs.lever.co/mistral"),
    ("cohere.com",          "Cohere",                   "https://boards.greenhouse.io/cohere"),
    ("groq.com",            "Groq",                     "https://boards.greenhouse.io/groq"),
    ("together.ai",         "Together AI",              "https://jobs.lever.co/togetherai"),
    ("perplexity.ai",       "Perplexity AI",            "https://boards.greenhouse.io/perplexityai"),
    ("xai.com",             "xAI (Grok)",               ""),
    ("inflection.ai",       "Inflection AI",            ""),
    ("imbue.com",           "Imbue AI",                 ""),
    ("adept.ai",            "Adept AI",                 ""),
    ("aleph-alpha.com",     "Aleph Alpha",              ""),
    ("characterai.com",     "Character AI",             ""),
    ("elevenlabs.io",       "ElevenLabs",               ""),
    ("runway.ml",           "Runway ML",                ""),
    ("midjourney.com",      "Midjourney",               ""),
    ("pika.art",            "Pika Labs",                ""),
    ("synthesia.io",        "Synthesia",                ""),
    ("stability.ai",        "Stability AI",             ""),
    ("ideogram.ai",         "Ideogram",                 ""),
    ("suno.com",            "Suno AI",                  ""),
    ("luma.ai",             "Luma AI",                  ""),

    # ─── FAANG-ADJACENT / BIG TECH AI ────────────────────────────────────────
    ("databricks.com",      "Databricks",               "https://boards.greenhouse.io/databricks"),
    ("snowflake.com",       "Snowflake",                "https://boards.greenhouse.io/snowflake"),
    ("palantir.com",        "Palantir",                 "https://jobs.lever.co/palantir"),
    ("salesforce.com",      "Salesforce",               "https://careers.salesforce.com"),
    ("adobe.com",           "Adobe",                    "https://apply.workable.com/adobe"),
    ("tesla.com",           "Tesla",                    ""),
    ("spacex.com",          "SpaceX",                   ""),
    ("oracle.com",          "Oracle",                   ""),
    ("ibm.com",             "IBM",                      ""),
    ("intel.com",           "Intel",                    ""),
    ("qualcomm.com",        "Qualcomm",                 ""),
    ("amd.com",             "AMD",                      ""),
    ("arm.com",             "ARM",                      ""),

    # ─── ML INFRA & DEVELOPER TOOLS AI STARTUPS ──────────────────────────────
    ("anyscale.com",        "Anyscale",                 "https://jobs.lever.co/anyscale"),
    ("modal.com",           "Modal Labs",               "https://jobs.lever.co/modal-labs"),
    ("wandb.ai",            "Weights & Biases",         "https://boards.greenhouse.io/wandb"),
    ("replit.com",          "Replit",                   "https://boards.greenhouse.io/replit"),
    ("coreweave.com",       "CoreWeave",                "https://boards.greenhouse.io/coreweave"),
    ("lambdalabs.com",      "Lambda Labs",              ""),
    ("deepinfra.com",       "DeepInfra",                ""),
    ("replicate.com",       "Replicate",                ""),
    ("huggingface.co",      "Hugging Face",             "https://apply.workable.com/huggingface"),
    ("langchain.com",       "LangChain",                ""),
    ("llamaindex.ai",       "LlamaIndex",               ""),
    ("weaviate.io",         "Weaviate",                 "https://boards.greenhouse.io/weaviate"),
    ("qdrant.tech",         "Qdrant",                   ""),
    ("pinecone.io",         "Pinecone",                 "https://boards.greenhouse.io/pinecone"),
    ("chroma.com",          "Chroma",                   ""),
    ("vellum.ai",           "Vellum AI",                ""),
    ("brainlid.org",        "Nx AI",                    ""),
    ("contextual.ai",       "Contextual AI",            ""),
    ("comet.ml",            "Comet ML",                 ""),
    # Scale AI is in block_companies filter — skipped intentionally
    ("aisera.com",          "Aisera",                   ""),
    ("glean.com",           "Glean",                    "https://apply.workable.com/glean"),
    ("moveworks.com",       "Moveworks",                "https://boards.greenhouse.io/moveworks"),
    ("cohere.com",          "Cohere",                   "https://boards.greenhouse.io/cohere"),
    ("vapi.ai",             "Vapi AI",                  ""),
    ("cresta.com",          "Cresta AI",                "https://jobs.lever.co/cresta"),
    ("jasper.ai",           "Jasper AI",                ""),
    ("writesonic.com",      "Writesonic",               ""),
    ("cursor.com",          "Cursor (Anysphere)",       ""),
    ("github.com",          "GitHub (Microsoft)",       "https://boards.greenhouse.io/github"),
    ("deepgram.com",        "Deepgram",                 "https://boards.greenhouse.io/deepgram"),
    ("assemblyai.com",      "AssemblyAI",               "https://jobs.lever.co/assemblyai"),
    ("covariant.ai",        "Covariant AI (Robotics)",  ""),
    ("physical-intelligence.ai", "Physical Intelligence", ""),
    ("figure.ai",           "Figure AI (Robotics)",     ""),
    ("abridge.com",         "Abridge",                  "https://boards.greenhouse.io/abridge"),

    # ─── QUANT / HIGH-FREQUENCY TRADING FIRMS ────────────────────────────────
    ("citadel.com",         "Citadel",                  ""),
    ("citadelsecurities.com","Citadel Securities",      ""),
    ("twosigma.com",        "Two Sigma",                ""),
    ("deshaw.com",          "DE Shaw",                  ""),
    ("janestreet.com",      "Jane Street",              ""),
    ("optiver.com",         "Optiver",                  ""),
    ("hrt.com",             "Hudson River Trading",     ""),
    ("jumptrading.com",     "Jump Trading",             ""),
    ("imc.com",             "IMC Trading",              ""),
    ("akunacapital.com",    "Akuna Capital",            ""),
    ("sig.com",             "Susquehanna (SIG)",        ""),
    ("worldquant.com",      "WorldQuant",               ""),
    ("point72.com",         "Point72",                  ""),
    ("tower-research.com",  "Tower Research Capital",   ""),
    ("virtu.com",           "Virtu Financial",          ""),
    ("drstonex.com",        "DRW",                      ""),
    ("drw.com",             "DRW",                      ""),
    ("cubistcapital.com",   "Cubist / Point72",         ""),
    ("millennium.com",      "Millennium Management",    ""),
    ("marsoncapital.com",   "Marson Capital",           ""),
    ("gsam.com",            "Goldman Sachs Asset Mgmt", ""),
    ("rentech.com",         "Renaissance Technologies", ""),
    ("iextrading.com",      "IEX",                      "https://iex.io/careers"),

    # ─── INVESTMENT BANKS / FINTECH ───────────────────────────────────────────
    ("goldmansachs.com",    "Goldman Sachs",            ""),
    ("jpmorgan.com",        "JPMorgan Chase",           ""),
    ("morganstanley.com",   "Morgan Stanley",           ""),
    ("bloomberg.com",       "Bloomberg",                ""),
    ("blackrock.com",       "BlackRock",                ""),
    ("citgroup.com",        "Citigroup",                ""),
    ("barclays.com",        "Barclays",                 ""),
    ("ubs.com",             "UBS",                      ""),
    ("wellsfargo.com",      "Wells Fargo",              ""),
    ("bankofamerica.com",   "Bank of America",          ""),
    ("fidelity.com",        "Fidelity Investments",     ""),
    ("vanguard.com",        "Vanguard",                 ""),
    ("statestreet.com",     "State Street",             ""),
    ("pimco.com",           "PIMCO",                    ""),
    ("bridgewater.com",     "Bridgewater Associates",   ""),
    ("kkr.com",             "KKR",                      ""),
    ("apolloglobal.com",    "Apollo Global",            ""),

    # ─── FINTECH STARTUPS ─────────────────────────────────────────────────────
    ("robinhood.com",       "Robinhood",                "https://boards.greenhouse.io/robinhood"),
    ("plaid.com",           "Plaid",                    "https://boards.greenhouse.io/plaid"),
    ("block.xyz",           "Block (Square)",           "https://boards.greenhouse.io/block"),
    ("coinbase.com",        "Coinbase",                 "https://boards.greenhouse.io/coinbase"),
    ("brex.com",            "Brex",                     "https://boards.greenhouse.io/brex"),
    ("ripple.com",          "Ripple",                   ""),
    ("chime.com",           "Chime",                    "https://boards.greenhouse.io/chime"),
    ("affirm.com",          "Affirm",                   "https://boards.greenhouse.io/affirm"),
    ("klarna.com",          "Klarna",                   ""),
    ("nerdwallet.com",      "NerdWallet",               "https://boards.greenhouse.io/nerdwallet"),

    # ─── HIGH-GROWTH CONSUMER/PLATFORM TECH ──────────────────────────────────
    ("uber.com",            "Uber",                     "https://boards.greenhouse.io/uber"),
    ("airbnb.com",          "Airbnb",                   "https://boards.greenhouse.io/airbnb"),
    ("doordash.com",        "DoorDash",                 "https://boards.greenhouse.io/doordash"),
    ("lyft.com",            "Lyft",                     "https://boards.greenhouse.io/lyft"),
    ("instacart.com",       "Instacart",                "https://boards.greenhouse.io/instacart"),
    ("roblox.com",          "Roblox",                   "https://boards.greenhouse.io/roblox"),
    ("figma.com",           "Figma (Adobe)",            "https://boards.greenhouse.io/figma"),
    ("notion.so",           "Notion",                   "https://boards.greenhouse.io/notion"),
    ("airtable.com",        "Airtable",                 "https://boards.greenhouse.io/airtable"),
    ("canva.com",           "Canva",                    ""),
    ("duolingo.com",        "Duolingo",                 "https://boards.greenhouse.io/duolingo"),
    ("quizlet.com",         "Quizlet",                  "https://boards.greenhouse.io/quizlet"),
    ("khan.org",            "Khan Academy",             ""),
    ("reddit.com",          "Reddit",                   "https://boards.greenhouse.io/reddit"),
    ("pinterest.com",       "Pinterest",                "https://boards.greenhouse.io/pinterest"),
    ("snap.com",            "Snap",                     "https://boards.greenhouse.io/snap"),
    ("x.com",               "X (Twitter)",              ""),
    ("spotify.com",         "Spotify",                  "https://boards.greenhouse.io/spotify"),
    ("twitch.tv",           "Twitch (Amazon)",          "https://boards.greenhouse.io/twitch"),
    ("discord.com",         "Discord",                  "https://boards.greenhouse.io/discord"),
    ("slack.com",           "Slack (Salesforce)",       "https://boards.greenhouse.io/slack"),

    # ─── CLOUD / ENTERPRISE INFRA ─────────────────────────────────────────────
    ("cloudflare.com",      "Cloudflare",               "https://boards.greenhouse.io/cloudflare"),
    ("datadog.com",         "Datadog",                  "https://boards.greenhouse.io/datadog"),
    ("elastic.co",          "Elastic",                  "https://boards.greenhouse.io/elastic"),
    ("mongodb.com",         "MongoDB",                  "https://boards.greenhouse.io/mongodb"),
    ("confluent.io",        "Confluent",                "https://boards.greenhouse.io/confluent"),
    ("hashicorp.com",       "HashiCorp (IBM)",          "https://boards.greenhouse.io/hashicorp"),
    ("dbt.com",             "dbt Labs",                 "https://boards.greenhouse.io/dbtlabs"),
    ("fivetran.com",        "Fivetran",                 "https://boards.greenhouse.io/fivetran"),
    ("starburst.io",        "Starburst",                "https://boards.greenhouse.io/starburst"),
    ("clickhouse.com",      "ClickHouse",               "https://boards.greenhouse.io/clickhouse"),
]


def main():
    conn = sqlite3.connect(str(DB_PATH))
    now = datetime.now(timezone.utc).isoformat()

    added = 0
    skipped = 0
    for domain, name, careers_url in COMPANIES:
        domain = domain.lower()
        existing = conn.execute(
            "SELECT careers_url FROM companies WHERE domain=?", (domain,)
        ).fetchone()

        if existing:
            # Only update careers_url if we have one and it's currently empty
            if careers_url and not existing[0]:
                conn.execute(
                    "UPDATE companies SET careers_url=?, name=? WHERE domain=?",
                    (careers_url, name, domain),
                )
                print(f"  UPDATED  {domain:45} careers_url → {careers_url}")
            else:
                skipped += 1
        else:
            conn.execute(
                """INSERT INTO companies (domain, name, first_seen, careers_url)
                   VALUES (?, ?, ?, ?)""",
                (domain, name, now, careers_url),
            )
            print(f"  ADDED    {domain:45} {careers_url or '(will auto-discover)'}")
            added += 1

    conn.commit()
    conn.close()
    print(f"\nDone — {added} new companies added, {skipped} already present.")


if __name__ == "__main__":
    main()
