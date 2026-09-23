import { mkdirSync, readFileSync, writeFileSync } from "fs";
import { dirname, join } from "path";
import { fileURLToPath } from "url";

const root = dirname(fileURLToPath(import.meta.url));
const taxonomy = JSON.parse(readFileSync(join(root, "../../../src/data/taxonomy.json"), "utf8"));
const byId = new Map(taxonomy.roles.map((role) => [role.id, role]));

const ROLE_SKILLS = {
  "machine-learning-engineer": ["python", "pytorch", "tensorflow", "machine learning", "deep learning", "sql"],
  "data-scientist": ["python", "sql", "statistics", "machine learning", "pandas", "pytorch"],
  "software-engineer": ["javascript", "typescript", "react", "sql", "aws", "docker"],
  "devops-engineer": ["docker", "kubernetes", "aws", "linux", "python", "gcp"],
  "data-engineer": ["python", "sql", "spark", "aws", "airflow", "dbt"],
  "data-analyst": ["sql", "excel", "tableau", "statistics", "python", "pandas"],
  "product-manager": ["research", "sql", "excel", "statistics", "design", "python"],
  "product-designer": ["figma", "design", "research", "javascript", "react", "excel"],
  "registered-nurse": ["nursing", "patient", "triage", "charting", "excel", "research"],
  "nurse-practitioner": ["nursing", "patient", "triage", "charting", "research", "excel"],
  "licensed-practical-nurse": ["nursing", "patient", "charting", "triage", "excel", "research"],
  electrician: ["electrical", "wiring", "hvac", "excel", "forklift", "welding"],
  plumber: ["plumbing", "welding", "hvac", "wiring", "excel", "forklift"],
  carpenter: ["wiring", "welding", "excel", "forklift", "design", "hvac"],
  welder: ["welding", "electrical", "forklift", "excel", "hvac", "wiring"],
  "hvac-technician": ["hvac", "electrical", "wiring", "plumbing", "excel", "forklift"],
  accountant: ["accounting", "gaap", "audit", "excel", "sql", "statistics"],
  "financial-analyst": ["excel", "statistics", "sql", "accounting", "gaap", "research"],
  bookkeeper: ["accounting", "excel", "gaap", "audit", "sql", "statistics"],
  auditor: ["audit", "accounting", "gaap", "excel", "sql", "statistics"],
  teacher: ["teaching", "curriculum", "classroom", "counseling", "excel", "research"],
  professor: ["teaching", "research", "curriculum", "classroom", "statistics", "excel"],
  "teacher-assistant": ["teaching", "classroom", "curriculum", "counseling", "excel", "research"],
  "school-counselor": ["counseling", "teaching", "classroom", "curriculum", "excel", "research"],
  cook: ["cooking", "hospitality", "guest", "excel", "forklift", "retail"],
  chef: ["cooking", "hospitality", "guest", "excel", "retail", "research"],
  waiter: ["hospitality", "guest", "cooking", "retail", "excel", "sales"],
  "hotel-manager": ["hospitality", "guest", "excel", "sales", "cooking", "retail"],
  "sales-representative": ["sales", "excel", "retail", "guest", "sql", "research"],
  "truck-driver": ["cdl", "logistics", "forklift", "excel", "guest", "sales"],
};

const BASES = [
  "machine-learning-engineer", "data-scientist", "software-engineer", "devops-engineer",
  "data-engineer", "data-analyst", "product-manager", "product-designer",
  "registered-nurse", "nurse-practitioner", "licensed-practical-nurse",
  "electrician", "plumber", "carpenter", "welder", "hvac-technician",
  "accountant", "financial-analyst", "bookkeeper", "auditor",
  "teacher", "professor", "teacher-assistant", "school-counselor",
  "cook", "chef", "waiter", "hotel-manager", "sales-representative", "truck-driver",
];

const FAR = {
  software: ["nursing", "patient", "triage", "charting"],
  health: ["pytorch", "tensorflow", "kubernetes", "docker"],
  trades: ["nursing", "gaap", "pytorch", "curriculum"],
  finance: ["nursing", "pytorch", "welding", "cooking"],
  education: ["pytorch", "welding", "nursing", "kubernetes"],
  hospitality: ["pytorch", "gaap", "nursing", "kubernetes"],
  sales: ["pytorch", "nursing", "welding", "curriculum"],
  logistics: ["pytorch", "nursing", "gaap", "curriculum"],
  design: ["nursing", "welding", "gaap", "kubernetes"],
};

function titles(role, band) {
  const adjacent = (role.adjacent || []).map((row) => ({ ...byId.get(row.id), weight: row.weight })).filter((row) => row.id);
  if (band === "strong") return [role.title, ...(role.synonyms || [])];
  if (band === "good") {
    const high = adjacent.filter((row) => row.weight >= 0.75);
    return (high.length ? high : adjacent).map((row) => row.title);
  }
  const low = adjacent.filter((row) => row.weight < 0.75);
  return (low.length ? low : adjacent).map((row) => row.title);
}

function pick(list, index) {
  return list[index % list.length];
}

function job(persona, band, index, title, required, preferred = []) {
  const id = `${persona.id}-${band}-${index}`;
  return {
    id,
    label: band,
    title,
    company: `Fixture ${persona.id} ${index}`,
    location_raw: "United States",
    locations: [{ country: "United States" }],
    posted_at: "2026-09-20T00:00:00Z",
    years_required: persona.years,
    seniority: "mid",
    role_id: "",
    skills_required: required,
    skills_preferred: preferred,
    description_text: `${title}. Required: ${required.join(", ")}. ${preferred.length ? `Preferred: ${preferred.join(", ")}.` : ""} Posting ${id}.`,
    url: `https://example.com/${id}`,
    status: "new",
  };
}

function irrelevantTitles(role) {
  return taxonomy.roles
    .filter((other) => other.domain !== role.domain && !(role.adjacent || []).some((row) => row.id === other.id))
    .map((other) => other.title);
}

function personaFrom(roleId, variant) {
  const role = byId.get(roleId);
  const years = variant === 0 ? 4 : 8;
  const start = 2026 - years;
  const skills = (ROLE_SKILLS[roleId] || []).slice(0, 4);
  const id = variant === 0 ? roleId : `${roleId}-${years}`;
  return {
    id,
    domain: role.domain,
    roles: [role.title],
    years,
    skills,
    resume_text: [
      "Jordan Lee",
      role.title,
      `${start} - 2026`,
      `${role.title} in ${role.domain}.`,
      skills.join(", "),
    ].join("\n"),
  };
}

function mlPersona() {
  const skills = ["python", "pytorch", "tensorflow", "machine learning", "deep learning", "sql", "statistics", "pandas"];
  return {
    id: "ml-automotive",
    domain: "software",
    roles: ["Machine Learning Engineer", "Data Scientist"],
    years: 8,
    skills,
    resume_text: [
      "Avery Chen",
      "Machine Learning Engineer",
      "Analytics Engineer",
      "2018 - 2026",
      "Machine learning and analytics engineer with automotive experience.",
      "Built models for vehicle programs.",
      skills.join(", "),
    ].join("\n"),
  };
}

function jobsFor(persona, role) {
  const own = persona.skills.slice(0, 4);
  const far = FAR[role.domain] || FAR.software;
  const strongTitles = titles(role, "strong");
  const goodTitles = titles(role, "good");
  const stretchTitles = titles(role, "stretch");
  const otherTitles = irrelevantTitles(role);
  const rows = [];
  for (let i = 0; i < 8; i += 1) {
    rows.push(job(persona, "strong", i, pick(strongTitles, i), own.slice()));
  }
  for (let i = 0; i < 10; i += 1) {
    rows.push(job(persona, "good", i, pick(goodTitles, i), own.slice(0, 3).concat(far[0])));
  }
  for (let i = 0; i < 8; i += 1) {
    rows.push(job(persona, "stretch", i, pick(stretchTitles, i), own.slice(0, 2).concat(far.slice(0, 2))));
  }
  for (let i = 0; i < 24; i += 1) {
    rows.push(job(persona, "irrelevant", i, pick(otherTitles, i), far.slice()));
  }
  return rows;
}

function mlJobs(persona) {
  const own = ["python", "pytorch", "tensorflow", "machine learning"];
  const far = FAR.software;
  const strong = [
    "Machine Learning Engineer", "ML Engineer", "MLE", "AI Engineer",
    "Data Scientist", "ML Scientist", "Senior Machine Learning Engineer", "Staff Data Scientist",
  ];
  const good = [
    "Applied Scientist", "Applied Science Researcher", "Research Scientist", "Research Scientist II",
    "Applied Scientist", "Research Scientist", "Applied Scientist", "Research Scientist",
    "Data Scientist", "Machine Learning Engineer",
  ];
  const stretch = [
    "MLOps Engineer", "Computer Vision Engineer", "Research Engineer", "Data Engineer",
    "MLOps Engineer", "CV Engineer", "Research Software Engineer", "Data Platform Engineer",
  ];
  const other = ["DevOps Engineer", "Sales Manager", "Account Executive", "Registered Nurse", "Cook", "Electrician", "Accountant", "Teacher", "Cashier", "Truck Driver"];
  const rows = [];
  strong.forEach((title, i) => rows.push(job(persona, "strong", i, title, own.slice())));
  good.forEach((title, i) => rows.push(job(persona, "good", i, title, own.slice(0, 3).concat(far[0]))));
  stretch.forEach((title, i) => rows.push(job(persona, "stretch", i, title, own.slice(0, 2).concat(far.slice(0, 2)))));
  for (let i = 0; i < 24; i += 1) rows.push(job(persona, "irrelevant", i, pick(other, i), far.slice()));
  return rows;
}

const personas = BASES.flatMap((roleId) => [0, 1].map((variant) => {
  if (roleId === "machine-learning-engineer" && variant === 0) {
    const persona = mlPersona();
    return { ...persona, jobs: mlJobs(persona) };
  }
  const persona = personaFrom(roleId, variant);
  return { ...persona, jobs: jobsFor(persona, byId.get(roleId)) };
}));

if (personas.length !== 60) throw new Error(`expected 60 personas, got ${personas.length}`);
personas.forEach((persona) => {
  const counts = {};
  persona.jobs.forEach((row) => { counts[row.label] = (counts[row.label] || 0) + 1; });
  if (persona.jobs.length !== 50 || counts.strong !== 8 || counts.good !== 10 || counts.stretch !== 8 || counts.irrelevant !== 24) {
    throw new Error(`${persona.id} labels ${JSON.stringify(counts)}`);
  }
});

mkdirSync(root, { recursive: true });
writeFileSync(join(root, "personas.json"), JSON.stringify({
  attribution: taxonomy.attribution,
  personas,
}, null, 0));
console.log(`wrote ${personas.length} personas`);
