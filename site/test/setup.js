import { readFileSync } from "node:fs";
import { setLexicon } from "../src/logic/lexicon.js";

// A fixed copy of the vocabulary learned from the September 2026 feed, so results do not move when
// the live feed is rebuilt every six hours.
setLexicon(JSON.parse(readFileSync(new URL("./fixtures/lexicon/lexicon.json", import.meta.url), "utf8")));
