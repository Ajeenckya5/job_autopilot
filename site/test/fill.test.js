// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { fillForm } from "../../extension/fill.js";

describe("autofill", () => {
  it("fills and highlights fields and does not submit", () => {
    document.body.innerHTML = `
      <form id="application">
        <label for="full">Full name</label>
        <input id="full" name="name">
        <label for="mail">Email</label>
        <input id="mail" type="email">
        <label for="secret">Password</label>
        <input id="secret" type="password">
        <button type="submit">Submit application</button>
      </form>`;
    const form = document.getElementById("application");
    let submitted = 0;
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      submitted += 1;
    });
    const filled = fillForm(document, { name: "Ada Lovelace", email: "ada@example.com", phone: "555" });
    const name = document.getElementById("full");
    const email = document.getElementById("mail");
    expect(filled).toBe(2);
    expect(name.value).toBe("Ada Lovelace");
    expect(email.value).toBe("ada@example.com");
    expect(name.style.outline).toContain("2px");
    expect(email.style.outline).toContain("2px");
    expect(document.getElementById("secret").value).toBe("");
    expect(submitted).toBe(0);
  });
});
