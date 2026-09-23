const MASTER = "llm-master";
const SEALED = "llm-sealed";

async function masterKey(storage) {
  const existing = await storage.get(MASTER);
  if (existing) return existing;
  const key = await crypto.subtle.generateKey({ name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
  await storage.set(MASTER, key);
  return key;
}

export async function sealSecret(storage, record) {
  const key = await masterKey(storage);
  if (key.extractable) throw new Error("The encryption key must stay on this device.");
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const cipher = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv },
    key,
    new TextEncoder().encode(JSON.stringify(record || {})),
  );
  await storage.set(SEALED, {
    iv: Array.from(iv),
    cipher: Array.from(new Uint8Array(cipher)),
  });
}

export async function openSecret(storage) {
  const sealed = await storage.get(SEALED);
  if (!sealed) return null;
  const key = await masterKey(storage);
  const iv = new Uint8Array(sealed.iv);
  const cipher = new Uint8Array(sealed.cipher);
  const plain = await crypto.subtle.decrypt({ name: "AES-GCM", iv }, key, cipher);
  return JSON.parse(new TextDecoder().decode(plain));
}

export async function macSaveKey(provider, apiKey, fetchImpl = fetch) {
  const response = await fetchImpl("/api/llm/key", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ provider, apiKey }),
  });
  if (!response.ok) throw new Error("The Mac app could not save the key.");
  return response.json();
}

export async function macKeyStatus(fetchImpl = fetch) {
  const response = await fetchImpl("/api/llm/key");
  if (!response.ok) return { providers: {} };
  return response.json();
}

export async function migrateMacKeys(fetchImpl = fetch) {
  const response = await fetchImpl("/api/llm/migrate", { method: "POST" });
  if (!response.ok) return { migrated: [] };
  return response.json();
}
