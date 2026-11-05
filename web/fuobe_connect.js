// Fuobe Connect in ComfyUI's page: a setting for the studios that may use this ComfyUI, and "Connect to Fuobe"
// (pairing: a short code you approve on the studio, for Safari, phones and PCs with no screen).
// Every value from the studio or the server goes into the page as text, never as markup.
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const SETTING = "FuobeConnect.Studios";
const DEFAULT_STUDIO = "https://fuobe.com";

const call = (path, init) => (api?.fetchApi ? api.fetchApi(path, init) : fetch(path, init));
const get = async () => {
  const res = await call("/fuobe/connect", { cache: "no-store" });
  if (!res.ok) throw new Error(`Fuobe Connect answered HTTP ${res.status}`);
  return res.json();
};
const post = async (path, body) => {
  const res = await call(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body ?? {}) });
  const json = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(json.error || `HTTP ${res.status}`);
  return json;
};

function toast(severity, detail) {
  const t = app.extensionManager?.toast;
  if (t?.add) t.add({ severity, summary: "Fuobe Connect", detail, life: 6000 });
  else (severity === "error" ? console.warn : console.info)("[Fuobe Connect]", detail);
}

/** A link from the studio, opened only when it is on that studio's own address. */
function sameSite(url, site) {
  try {
    const u = new URL(url);
    return (u.protocol === "https:" || u.protocol === "http:") && u.origin === new URL(site).origin;
  } catch {
    return false;
  }
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "style") Object.assign(node.style, v);
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children) node.append(c);
  return node;
}

let dialog = null;
let timer = null;

function close() {
  clearInterval(timer);
  timer = null;
  dialog?.remove();
  dialog = null;
}

const when = (ms) => {
  try {
    return new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  } catch {
    return "";
  }
};

function render(box, s) {
  box.replaceChildren();
  const line = (text, style = {}) => box.append(el("p", { style: { margin: "0 0 10px", ...style } }, String(text ?? "")));
  const small = { fontSize: "12px", opacity: "0.75" };
  const button = (label, onclick, primary) =>
    el(
      "button",
      {
        onclick: async (e) => {
          e.target.disabled = true;
          try {
            await onclick(e);
          } catch (err) {
            render(box, { ...s, message: String(err?.message || err) });
          }
        },
        style: {
          marginRight: "8px",
          padding: "6px 12px",
          borderRadius: "6px",
          border: "1px solid #666",
          cursor: "pointer",
          background: primary ? "#2563eb" : "transparent",
          color: primary ? "#fff" : "inherit",
        },
      },
      label,
    );
  const jobs = () => {
    if (!s.jobs?.length) return;
    box.append(el("p", { style: { margin: "12px 0 4px", fontWeight: "600" } }, "Last jobs"));
    const list = el("ul", { style: { margin: "0", padding: "0 0 0 16px", maxHeight: "160px", overflowY: "auto", ...small } });
    for (const j of s.jobs.slice(0, 10)) {
      const nodes = (j.nodes ?? []).slice(0, 6).join(", ") + ((j.nodes?.length ?? 0) > 6 || j.more ? ", …" : "");
      list.append(el("li", { style: { marginBottom: "4px" } }, `${when(j.at)} · ${j.state} · ${nodes}`));
    }
    box.append(list);
  };
  if (s.revokePending) {
    line(`Stopped here. Telling ${s.revokePending} to forget this computer…`, { fontWeight: "600" });
    line("You can also remove this computer in Fuobe's settings.", small);
    box.append(button("Close", close));
    return;
  }
  if (s.paired && s.state !== "unpaired") {
    line(`Connected to ${s.paired.site}.`, { fontWeight: "600" });
    if (s.paired.account) line(`Approved by ${s.paired.account}.`);
    if (s.paired.name) line(`This computer is “${s.paired.name}” on Fuobe.`, small);
    line(s.paused ? "Fuobe jobs are paused on this computer." : s.state === "connected" ? "Your Fuobe jobs run on this ComfyUI, even with no Fuobe tab open." : s.message || "Waiting for the studio…");
    box.append(
      button(s.paused ? "Resume jobs" : "Pause jobs", async () => render(box, await post("/fuobe/connect/jobs", { paused: !s.paused }))),
      button("Disconnect", async () => {
        await post("/fuobe/connect/unpair");
        render(box, await get());
      }),
      button("Close", close),
    );
    jobs();
    return;
  }
  if (s.state === "pairing" && s.pairing) {
    line("Approve this code on Fuobe, signed in:");
    box.append(el("p", { style: { fontSize: "28px", letterSpacing: "4px", fontFamily: "monospace", margin: "4px 0 12px" } }, String(s.pairing.code ?? "")));
    const ok = sameSite(s.pairing.url, s.pairing.site);
    if (ok) box.append(button("Open Fuobe to approve", () => window.open(s.pairing.url, "_blank", "noopener,noreferrer"), true));
    box.append(
      button("Cancel", async () => {
        await post("/fuobe/connect/unpair");
        close();
      }),
    );
    if (ok) line(`Or open ${s.pairing.url} on any device.`, { marginTop: "10px", ...small });
    line("Only approve a code you see here, on your own Fuobe account.", small);
    return;
  }
  line("Run your Fuobe jobs on this ComfyUI, with no browser tab open. Useful for Safari, phones and PCs with no screen.");
  if (s.message) line(s.message, small);
  const site = s.pairSites?.[0] ?? s.studios?.[0] ?? DEFAULT_STUDIO;
  const name = el("input", { type: "text", maxlength: "48", placeholder: "This computer's name (optional)", style: { width: "100%", boxSizing: "border-box", margin: "0 0 10px", padding: "6px 8px", borderRadius: "6px", border: "1px solid #666", background: "transparent", color: "inherit" } });
  box.append(name);
  let host = site;
  try {
    host = new URL(site).host;
  } catch {}
  box.append(
    button(`Connect to ${host}`, async () => render(box, await post("/fuobe/connect/pair", { site, ...(name.value.trim() && { name: name.value.trim() }) })), true),
    button("Close", close),
  );
  jobs();
}

async function refresh(box) {
  try {
    render(box, await get());
  } catch (err) {
    box.replaceChildren(el("p", { style: { margin: "0 0 10px" } }, `Fuobe Connect isn't answering: ${String(err?.message || err)}`));
  }
}

async function open() {
  close();
  const box = el("div", {});
  dialog = el(
    "div",
    {
      role: "dialog",
      "aria-label": "Fuobe Connect",
      style: { position: "fixed", top: "80px", right: "24px", zIndex: "10000", width: "380px", maxWidth: "calc(100vw - 48px)", padding: "16px", borderRadius: "10px", background: "var(--comfy-menu-bg, #222)", color: "var(--fg-color, #ddd)", boxShadow: "0 8px 30px rgba(0,0,0,.4)", font: "14px system-ui, sans-serif" },
    },
    el("p", { style: { margin: "0 0 10px", fontWeight: "700" } }, "Fuobe Connect"),
    box,
  );
  document.body.append(dialog);
  await refresh(box);
  timer = setInterval(() => {
    // Don't redraw while the person types a name.
    if (dialog && !box.contains(document.activeElement)) refresh(box).catch(() => {});
  }, 2000);
}

// The setting shows what Fuobe Connect really uses; a change it refuses is put back, with a note.
let syncing = false;
const settings = () => app.extensionManager?.setting;
const shown = (s) => (s.setting ?? (s.studios?.length ? s.studios.filter((o) => !/^https:\/\/www\./.test(o) || !s.studios.includes(o.replace("://www.", "://"))).join(", ") : "none"));

async function sync() {
  try {
    const s = await get();
    if (s.fromEnv) toast("info", `FUOBE_CONNECT_ORIGINS is set, so these studios are used: ${(s.studios ?? []).join(", ") || "none"}. The setting changes nothing until it is removed.`);
    const value = shown(s);
    const st = settings();
    if (st?.get && st?.set && st.get(SETTING) !== value) {
      syncing = true;
      try {
        await st.set(SETTING, value);
      } finally {
        syncing = false;
      }
    }
  } catch {}
}

app.registerExtension({
  name: "Fuobe.Connect",
  settings: [
    {
      id: SETTING,
      category: ["Fuobe Connect", "Studios", "Studios"],
      name: "Fuobe studios that may use this ComfyUI",
      tooltip: "Their pages may use this ComfyUI from your browser, to run their jobs: https addresses, separated by commas. Type none to let no website in (pairing still works). Every other website stays out.",
      type: "text",
      defaultValue: DEFAULT_STUDIO,
      onChange: (value, old) => {
        if (syncing || old === undefined || value === old) return;
        post("/fuobe/connect/studios", { studios: value })
          .then((s) => toast("success", s.studios?.length ? `Trusted: ${s.studios.join(", ")}` : "No website may use this ComfyUI from the browser."))
          .catch((e) => {
            toast("error", String(e?.message || e));
            sync();
          });
      },
    },
  ],
  commands: [{ id: "FuobeConnect.Open", label: "Connect to Fuobe", icon: "pi pi-link", function: open }],
  menuCommands: [{ path: ["Extensions"], commands: ["FuobeConnect.Open"] }],
  async setup() {
    await sync();
  },
});
