#!/usr/bin/env python3
"""Seed watched-company list with FAANG, AI labs, quant/trading firms,
investment banks, and high-signal AI startups.

Run once:  python3 seed_companies.py
"""
import sqlite3
import os
from datetime import datetime, timezone
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent


def _db_path() -> Path:
    cfg_path = HERE / "config.yaml"
    if cfg_path.exists():
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f) or {}
        out_dir = cfg.get("output_dir")
        if out_dir:
            return Path(os.path.expanduser(out_dir)).resolve() / "autopilot.sqlite"
    return HERE / "autopilot.sqlite"


DB_PATH = _db_path()

COMPANIES = [
    # ─── FAANG ────────────────────────────────────────────────────────────────
    ("amazon.com",          "Amazon",                   ""),
    ("microsoft.com",       "Microsoft",                "https://careers.microsoft.com"),

    # ─── AI LABS & FRONTIER MODEL COMPANIES ──────────────────────────────────
    ("openai.com",          "OpenAI",                   ""),
    ("anthropic.com",       "Anthropic",                "https://boards.greenhouse.io/anthropic"),
    ("mistral.ai",          "Mistral AI",               "https://jobs.lever.co/mistral"),
    ("cohere.com",          "Cohere",                   "https://jobs.ashbyhq.com/cohere"),
    ("groq.com",            "Groq",                     "https://groq.com/careers-at-groq"),
    ("together.ai",         "Together AI",              "https://job-boards.greenhouse.io/togetherai"),
    ("perplexity.ai",       "Perplexity AI",            "https://jobs.ashbyhq.com/perplexity"),
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
    ("snowflake.com",       "Snowflake",                "https://careers.snowflake.com/us/en"),
    ("palantir.com",        "Palantir",                 "https://jobs.lever.co/palantir"),
    ("salesforce.com",      "Salesforce",               "https://careers.salesforce.com"),
    ("adobe.com",           "Adobe",                    "https://careers.adobe.com/us/en"),
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
    ("modal.com",           "Modal Labs",               "https://jobs.ashbyhq.com/modal"),
    ("wandb.ai",            "Weights & Biases",         "https://wandb.ai/site/careers"),
    ("replit.com",          "Replit",                   "https://jobs.ashbyhq.com/replit"),
    ("coreweave.com",       "CoreWeave",                "https://boards.greenhouse.io/coreweave"),
    ("lambdalabs.com",      "Lambda Labs",              ""),
    ("deepinfra.com",       "DeepInfra",                ""),
    ("replicate.com",       "Replicate",                ""),
    ("huggingface.co",      "Hugging Face",             "https://apply.workable.com/huggingface"),
    ("langchain.com",       "LangChain",                ""),
    ("llamaindex.ai",       "LlamaIndex",               ""),
    ("weaviate.io",         "Weaviate",                 "https://jobs.ashbyhq.com/weaviate"),
    ("qdrant.tech",         "Qdrant",                   ""),
    ("pinecone.io",         "Pinecone",                 "https://jobs.ashbyhq.com/pinecone"),
    ("chroma.com",          "Chroma",                   ""),
    ("vellum.ai",           "Vellum AI",                ""),
    ("brainlid.org",        "Nx AI",                    ""),
    ("contextual.ai",       "Contextual AI",            ""),
    ("comet.ml",            "Comet ML",                 ""),
    # Scale AI is in block_companies filter — skipped intentionally
    ("aisera.com",          "Aisera",                   ""),
    ("glean.com",           "Glean",                    "https://jobs.ashbyhq.com/glean"),
    ("moveworks.com",       "Moveworks",                "https://boards.greenhouse.io/moveworks"),
    ("vapi.ai",             "Vapi AI",                  ""),
    ("cresta.com",          "Cresta AI",                "https://job-boards.greenhouse.io/cresta"),
    ("jasper.ai",           "Jasper AI",                ""),
    ("writesonic.com",      "Writesonic",               ""),
    ("cursor.com",          "Cursor (Anysphere)",       ""),
    ("github.com",          "GitHub (Microsoft)",       "https://www.github.careers/careers-home/jobs"),
    ("deepgram.com",        "Deepgram",                 "https://deepgram.com/careers"),
    ("assemblyai.com",      "AssemblyAI",               "https://boards.greenhouse.io/assemblyai"),
    ("covariant.ai",        "Covariant AI (Robotics)",  ""),
    ("physical-intelligence.ai", "Physical Intelligence", ""),
    ("figure.ai",           "Figure AI (Robotics)",     ""),
    ("abridge.com",         "Abridge",                  "https://jobs.ashbyhq.com/abridge"),

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
    ("drw.com",             "DRW",                      "https://www.drw.com/work-at-drw/listings"),
    ("cubistcapital.com",   "Cubist / Point72",         ""),
    ("millennium.com",      "Millennium Management",    ""),
    ("marsoncapital.com",   "Marson Capital",           ""),
    ("gsam.com",            "Goldman Sachs Asset Mgmt", ""),
    ("rentec.com",          "Renaissance Technologies", ""),
    ("iextrading.com",      "IEX",                      "https://iex.io/careers"),

    # ─── INVESTMENT BANKS / FINTECH ───────────────────────────────────────────
    ("goldmansachs.com",    "Goldman Sachs",            ""),
    ("jpmorgan.com",        "JPMorgan Chase",           ""),
    ("morganstanley.com",   "Morgan Stanley",           ""),
    ("bloomberg.com",       "Bloomberg",                ""),
    ("blackrock.com",       "BlackRock",                ""),
    ("citi.com",            "Citigroup",                "https://jobs.citi.com"),
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
    ("plaid.com",           "Plaid",                    "https://plaid.com/careers"),
    ("block.xyz",           "Block (Square)",           "https://boards.greenhouse.io/block"),
    ("coinbase.com",        "Coinbase",                 "https://www.coinbase.com/careers"),
    ("brex.com",            "Brex",                     "https://boards.greenhouse.io/brex"),
    ("ripple.com",          "Ripple",                   ""),
    ("chime.com",           "Chime",                    "https://boards.greenhouse.io/chime"),
    ("affirm.com",          "Affirm",                   "https://boards.greenhouse.io/affirm"),
    ("klarna.com",          "Klarna",                   ""),
    ("nerdwallet.com",      "NerdWallet",               "https://www.nerdwallet.com/careers/jobs"),

    # ─── HIGH-GROWTH CONSUMER/PLATFORM TECH ──────────────────────────────────
    ("uber.com",            "Uber",                     "https://jobs.uber.com/en"),
    ("airbnb.com",          "Airbnb",                   "https://boards.greenhouse.io/airbnb"),
    ("doordash.com",        "DoorDash",                 "https://careersatdoordash.com/jobs"),
    ("lyft.com",            "Lyft",                     "https://boards.greenhouse.io/lyft"),
    ("instacart.com",       "Instacart",                "https://boards.greenhouse.io/instacart"),
    ("roblox.com",          "Roblox",                   "https://boards.greenhouse.io/roblox"),
    ("figma.com",           "Figma (Adobe)",            "https://boards.greenhouse.io/figma"),
    ("notion.so",           "Notion",                   "https://www.notion.com/careers"),
    ("airtable.com",        "Airtable",                 "https://boards.greenhouse.io/airtable"),
    ("canva.com",           "Canva",                    ""),
    ("duolingo.com",        "Duolingo",                 "https://boards.greenhouse.io/duolingo"),
    ("quizlet.com",         "Quizlet",                  "https://quizlet.com/jobs"),
    ("khan.org",            "Khan Academy",             ""),
    ("reddit.com",          "Reddit",                   "https://boards.greenhouse.io/reddit"),
    ("pinterest.com",       "Pinterest",                "https://boards.greenhouse.io/pinterest"),
    ("snap.com",            "Snap",                     "https://careers.snap.com/jobs"),
    ("x.com",               "X (Twitter)",              ""),
    ("spotify.com",         "Spotify",                  "https://www.lifeatspotify.com/jobs"),
    ("twitch.tv",           "Twitch (Amazon)",          "https://boards.greenhouse.io/twitch"),
    ("discord.com",         "Discord",                  "https://boards.greenhouse.io/discord"),
    ("slack.com",           "Slack (Salesforce)",       "https://slack.com/careers"),

    # ─── CLOUD / ENTERPRISE INFRA ─────────────────────────────────────────────
    ("cloudflare.com",      "Cloudflare",               "https://boards.greenhouse.io/cloudflare"),
    ("datadog.com",         "Datadog",                  "https://boards.greenhouse.io/datadog"),
    ("elastic.co",          "Elastic",                  "https://boards.greenhouse.io/elastic"),
    ("mongodb.com",         "MongoDB",                  "https://boards.greenhouse.io/mongodb"),
    ("confluent.io",        "Confluent",                "https://careers.confluent.io"),
    ("hashicorp.com",       "HashiCorp (IBM)",          "https://www.hashicorp.com/en/careers"),
    ("dbt.com",             "dbt Labs",                 "https://www.getdbt.com/about-us/careers"),
    ("fivetran.com",        "Fivetran",                 "https://boards.greenhouse.io/fivetran"),
    ("starburst.io",        "Starburst",                "https://boards.greenhouse.io/starburst"),
    ("clickhouse.com",      "ClickHouse",               "https://boards.greenhouse.io/clickhouse"),

    # ─── ADDED FROM TIER LIST — quant / HFT ──────────────────────────────────
    ("radix-trading.com",     "Radix Trading",          ""),
    ("arrowstreetcapital.com","Arrowstreet Capital",    ""),
    ("pdtpartners.com",       "PDT Partners",           ""),
    ("voleon.com",            "The Voleon Group",       ""),
    ("xtxmarkets.com",        "XTX Markets",            ""),
    ("aqr.com",               "AQR Capital Management",  "https://boards.greenhouse.io/aqr"),
    ("squarepoint-capital.com","Squarepoint Capital",   "https://job-boards.greenhouse.io/squarepointcapital"),
    ("vivcourt.com",          "VivCourt Trading",       ""),

    # ─── ADDED FROM TIER LIST — big tech / platforms ─────────────────────────
    ("nvidia.com",            "NVIDIA",                  ""),
    ("netflix.com",           "Netflix",                 ""),
    ("meta.com",              "Meta",                    ""),
    ("apple.com",             "Apple",                   ""),
    ("google.com",            "Google",                  ""),
    ("stripe.com",            "Stripe",                  ""),
    ("paypal.com",            "PayPal",                  ""),
    ("asana.com",             "Asana",                   ""),
    ("coupang.com",           "Coupang",                 "https://boards.greenhouse.io/coupang"),
    ("linkedin.com",          "LinkedIn",                ""),
    ("dropbox.com",           "Dropbox",                 ""),
    ("ebay.com",              "eBay",                    ""),
    ("atlassian.com",         "Atlassian",               ""),
    ("booking.com",           "Booking.com",             ""),

    # ─── ADDED FROM TIER LIST — finance / PE ─────────────────────────────────
    ("blackstone.com",        "Blackstone",              ""),
    ("capitalone.com",        "Capital One",             ""),

    # ─── ADDED FROM TIER LIST (round 2) — quant firms, scraping verified ──────
    ("tgsmc.com",             "TGS Management",          ""),
    ("quadrature.ai",         "Quadrature Capital",      ""),
    ("fiverings.com",         "Five Rings",              "https://boards.greenhouse.io/fiveringsllc"),
    ("schonfeld.com",         "Schonfeld Strategic Advisors", "https://boards.greenhouse.io/schonfeld"),
    ("mwam.com",              "Marshall Wace",           "https://boards.greenhouse.io/marshallwace"),
    ("gresearch.com",         "G-Research",              ""),
]


def main():
    import argparse
    ap = argparse.ArgumentParser(
        description="Seed the watched-company list into a track's autopilot.sqlite.")
    ap.add_argument("--db", default=str(DB_PATH),
                    help="Path to the track DB (default: derived from config.yaml).")
    args = ap.parse_args()

    # Use the real DB layer so the schema (and any migrations) are guaranteed and
    # upserts behave exactly like the running pipeline. This also creates a fresh
    # DB with the correct schema if one doesn't exist yet (e.g. after a clean start).
    try:
        import core
    except Exception as e:
        print(f"ERROR: cannot import core ({e}); run from the project folder.")
        raise SystemExit(1)

    db = core.DB(args.db)
    before = len(db.list_watched())
    for domain, name, careers_url in COMPANIES:
        db.watch(domain.lower().strip(), name, careers_url)
    db.conn.commit()
    after = len(db.list_watched())
    print(f"Watchlist seeded into {args.db}")
    print(f"  {after} companies total  ({after - before} new this run; "
          f"{len(COMPANIES)} in seed list).")


if __name__ == "__main__":
    main()
