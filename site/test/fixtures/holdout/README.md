# Matching holdout

330 real postings from the company feed (six people, 55 each), labelled by hand. The labels
were set before any score was computed, so the scorer cannot have shaped them.

## People

| Id | Target | Years |
|---|---|---|
| mle | Machine Learning Engineer | 4 |
| ds | Data Scientist | 3 |
| rn | Registered Nurse | 5 |
| swe | Software Engineer (backend) | 4 |
| pm | Product Manager | 5 |
| ie | Industrial Engineer, Supply Chain Analyst | 3 |

## Rubric

A posting is relevant when someone with this resume and target would reasonably apply:

- Same craft as the target. Adjacent crafts count as not relevant (a data engineer for a data
  scientist, SRE or DevOps for a backend engineer, a program manager for a product manager).
- Individual contributor up to Senior. Staff, Principal, Lead, Manager, Director, VP and Head are
  not relevant for these years of experience.
- Internships, co-ops, student and new-grad roles are not relevant; these people have jobs.
- A different license is not relevant (a nurse practitioner role for a registered nurse).
- Location, clearance and sponsorship are ignored here; the filters handle those.

Each person has 12 to 22 postings whose title looks like the target, about 15 adjacent titles,
and 18 unrelated ones, sampled with a fixed seed. 72 of 330 are relevant.

## Rules for changing it

Add rows, never flip a label to match the scorer. If a label is wrong by the rubric, fix it in a
separate commit that says which row and why.
