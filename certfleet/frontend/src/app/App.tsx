import { useState, useEffect, useCallback, useRef } from "react";
import {
  Shield, RefreshCw, Upload, CheckCircle2, AlertCircle,
  Terminal, Eye, EyeOff, Loader2, Activity,
  Wifi, WifiOff, Settings2, X, Download, HardDrive,
  Plus, Trash2, Edit2, Save, ChevronDown, Settings, Zap,
  FileWarning, FolderOpen, Copy, KeyRound, Bell,
} from "lucide-react";

// ── Types ─────────────────────────────────────────────────────────────────────

type DeviceType = "truenas" | "brother" | "hubitat" | "comware" | "omada" | "pfsense" | "proxmox";

interface LocalCert {
  domain: string; issuer: string; not_before: string; not_after: string;
  days_remaining: number; fingerprint: string; serial: string;
  cert_path: string; key_path: string; last_checked: string;
  key_info: string; sig_algorithm: string; sans: string[];
  root_ca: string; key_usage: string; is_staging: boolean;
}

interface Device {
  id: string; name: string; type: DeviceType; enabled: boolean; host: string;
  running: boolean; last_run: string | null;
  last_status: "already_current" | "deployed" | "needs_deploy" | "skipped" | "no_local_cert" | "error" | null;
  last_message: string | null; live_fingerprint: string | null;
  last_warning: string | null;
  pfsense_allow_upload?: boolean; proxmox_allow_upload?: boolean;
}

interface DeviceConfigEntry {
  id: string; name: string; type: DeviceType; enabled: boolean; host: string;
  port?: number; username?: string; password?: string; api_key?: string;
  site_id?: string; pki_domain?: string; ssl_policy?: string;
  startup_config_path?: string; verify_tls?: boolean;
  pfsense_allow_upload?: boolean; proxmox_allow_upload?: boolean; omadac_id?: string;
  p12_password?: string; delete_old_certs?: boolean;
}

interface AppConfig {
  devices: DeviceConfigEntry[];
  cert_path?: string;
  key_path?: string;
  notify_enabled?: boolean;
  poll_interval_ms?: number;
  auto_deploy_on_renewal?: boolean;
}

interface BackupState { status: "idle"|"running"|"done"|"error"; filename?: string; error?: string; }
interface LogEntry { id: number; ts: string; level: "info"|"success"|"warn"|"error"; msg: string; device: string|null; }
interface ConnectResult { ok: boolean; message: string; latency_ms: number; }

// ── Constants ─────────────────────────────────────────────────────────────────

const DEVICE_TYPES: { value: DeviceType; label: string; icon: string; defaultPort: number; namePlaceholder: string }[] = [
  { value: "truenas",  label: "TrueNAS (CORE / CE / Enterprise)", icon: "💾", defaultPort: 443, namePlaceholder: "My TrueNAS"   },
  { value: "pfsense",  label: "pfSense",                   icon: "🛡️", defaultPort: 443, namePlaceholder: "My pfSense"        },
  { value: "comware",  label: "HP Switch - 1950 Series",   icon: "🔀", defaultPort: 22,  namePlaceholder: "My HP Switch"      },
  { value: "hubitat",  label: "Hubitat C-7",               icon: "🏠", defaultPort: 443, namePlaceholder: "My Hubitat"        },
  { value: "omada",    label: "TP-Link Omada OC200/OC300", icon: "📡", defaultPort: 443, namePlaceholder: "My Omada OC200"    },
  { value: "brother",  label: "Brother MFC Printer",       icon: "🖨️", defaultPort: 443, namePlaceholder: "My Brother Printer"},
  { value: "proxmox",  label: "Proxmox VE",                icon: "🖥️", defaultPort: 8006, namePlaceholder: "My Proxmox Node"   },
];
const DEVICE_TYPE_MAP = Object.fromEntries(DEVICE_TYPES.map(d => [d.value, d]));

const TYPE_FIELDS: Record<DeviceType, string[]> = {
  truenas:  ["api_key", "verify_tls"],
  pfsense:  ["username", "password", "port", "pfsense_allow_upload", "ssl_policy", "pki_domain"],
  comware:  ["username", "password", "api_key", "port", "pki_domain", "ssl_policy", "startup_config_path"],
  hubitat:  ["username", "password", "port"],
  omada:    ["username", "password", "site_id", "omadac_id", "verify_tls"],
  brother:  ["password"],
  proxmox:  ["username", "api_key", "site_id", "port", "proxmox_allow_upload"],
};

const BG_PRESETS = [
  { label: "GitHub Dark",  value: "#0d1117" },
  { label: "Pitch Black",  value: "#000000" },
  { label: "Dark Navy",    value: "#080d1a" },
  { label: "Dark Slate",   value: "#0f172a" },
  { label: "Dark Teal",    value: "#071520" },
  { label: "Dark Forest",  value: "#0a1a0d" },
];

const DEFAULT_CERT_PATH = "/ssl/fullchain.pem";
const DEFAULT_KEY_PATH  = "/ssl/privkey.pem";
// Fallback only — real version comes from the Supervisor at runtime (see appVersion state).
const APP_VERSION_FALLBACK = "unknown";

// Geometric spacing, not linear — matches the cadence people expect from
// pickers like Windows Update, not an arbitrary "every N minutes" slider.
const POLL_INTERVALS: { label: string; value: number }[] = [
  { label: "1 minute",   value: 60_000 },
  { label: "5 minutes",  value: 300_000 },
  { label: "15 minutes", value: 900_000 },
  { label: "30 minutes", value: 1_800_000 },
  { label: "1 hour",     value: 3_600_000 },
  { label: "6 hours",    value: 21_600_000 },
  { label: "12 hours",   value: 43_200_000 },
  { label: "1 day",      value: 86_400_000 },
];
// Let's Encrypt certs are valid ~60-90 days — nothing here needs sub-minute
// freshness, so default to a calmer cadence than the old hardcoded 60s.
const DEFAULT_POLL_INTERVAL_MS = 900_000; // 15 min

// ── Helpers ───────────────────────────────────────────────────────────────────

// Shared "did something" feedback color — every button that confirms an
// action flashes this same green, matching the Refresh Local Cert Info
// button, so the whole app speaks one visual language for "it worked."
const FLASH_GREEN = "border-[#39d353] bg-[#39d353]/20 text-[#39d353]";

function slugify(s: string) {
  return s.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "");
}

// navigator.clipboard is unavailable in some contexts even over HTTPS —
// notably inside cross-origin iframes without an explicit permissions
// policy, which is exactly how Home Assistant embeds add-on ingress UIs.
// It fails *silently* (the optional-chained call just short-circuits to
// undefined, no error, no rejection), so callers can't tell the
// difference between "copied" and "nothing happened" without this
// fallback and an honest return value.
async function copyToClipboard(text: string): Promise<boolean> {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // fall through to the legacy method below
    }
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

// Backend timestamps are ISO 8601 with a UTC offset (e.g. "...+00:00") —
// Date correctly parses that as an absolute instant, so toLocaleString()
// renders it in whatever timezone the browser itself is set to, instead
// of displaying raw UTC mislabeled or unlabeled.
function formatLocalTime(iso: string): string {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
    hour12: false,
  });
}

function statusColor(s: Device["last_status"]) {
  switch (s) {
    case "already_current": return "text-[#39d353]";
    case "deployed":        return "text-[#1f6feb]";
    case "needs_deploy":    return "text-[#e3b341]";
    case "skipped":         return "text-[#8b949e]";
    case "no_local_cert":   return "text-[#58a6ff]";
    case "error":           return "text-[#f85149]";
    default:                return "text-[#8b949e]";
  }
}
function statusBg(s: Device["last_status"]) {
  switch (s) {
    case "already_current": return "bg-[#39d353]/10 border-[#39d353]/30";
    case "deployed":        return "bg-[#1f6feb]/10 border-[#1f6feb]/30";
    case "needs_deploy":    return "bg-[#e3b341]/10 border-[#e3b341]/30";
    case "skipped":         return "bg-[#30363d]/40 border-[#30363d]";
    case "no_local_cert":   return "bg-[#58a6ff]/10 border-[#58a6ff]/30";
    case "error":           return "bg-[#f85149]/10 border-[#f85149]/30";
    default:                return "bg-[#30363d]/40 border-[#30363d]";
  }
}
function statusLabel(s: Device["last_status"]) {
  switch (s) {
    case "already_current": return "In Sync";
    case "deployed":        return "Deployed";
    case "needs_deploy":    return "Needs Deploy";
    case "skipped":         return "Verify Only";
    case "no_local_cert":   return "Connected";
    case "error":           return "Error";
    default:                return "Not Checked";
  }
}
function shortFp(fp: string, pairs = 4): string {
  // "SHA256:AA:BB:CC:...:XX:YY:ZZ" → "SHA256:AA:BB:CC:DD:…:WW:XX:YY:ZZ"
  const parts = fp.split(":");
  if (parts.length <= pairs * 2 + 1) return fp;
  const prefix = parts.slice(0, pairs + 1).join(":");
  const suffix = parts.slice(-pairs).join(":");
  return `${prefix}:…:${suffix}`;
}

function validateHost(raw: string): string | null {
  const h = raw.trim().replace(/^https?:\/\//i, "").replace(/\/.*$/, "");
  if (!h) return "Hostname is required";
  // Allow letters, digits, dots, hyphens, underscores, colons (IPv6 / port), brackets (IPv6)
  if (!/^[\w.\-[\]:]+$/.test(h)) return "Invalid characters — use a hostname, IPv4, or IPv6 address";
  // Reject octets > 255 for things that look like IPv4
  if (/^\d+\.\d+(\.\d+)*$/.test(h)) {
    const octets = h.split(".");
    if (octets.some(o => parseInt(o, 10) > 255)) return "Invalid IPv4 address (octet > 255)";
    if (octets.length !== 4) return "Invalid IPv4 address (need 4 octets)";
  }
  return null;
}

function certDayColor(d: number) {
  return d <= 14 ? "text-[#f85149]" : d <= 30 ? "text-[#e3b341]" : "text-[#39d353]";
}
function logLevelColor(l: LogEntry["level"]) {
  switch (l) {
    case "success": return "text-[#39d353]";
    case "warn":    return "text-[#e3b341]";
    case "error":   return "text-[#f85149]";
    default:        return "text-[#6e7681]";
  }
}

// ── Shared UI primitives ──────────────────────────────────────────────────────

function Pill({ label, color }: { label: string; color: string }) {
  return (
    <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded border text-[13px] font-mono font-medium uppercase tracking-wider ${color}`}>
      <span className="size-1.5 rounded-full bg-current" />
      {label}
    </span>
  );
}

function TextInput({ value, onChange, placeholder, password, mono }: {
  value: string; onChange: (v: string) => void;
  placeholder?: string; password?: boolean; mono?: boolean;
}) {
  const [show, setShow] = useState(false);
  return (
    <div className="relative flex">
      <input
        type={password && !show ? "password" : "text"}
        value={value ?? ""}
        onChange={e => onChange(e.target.value)}
        placeholder={placeholder}
        className={`flex-1 bg-[#010409] border border-[#30363d] rounded px-3 py-2 text-[15px] text-[#e6edf3] placeholder-[#484f58] focus:outline-none focus:border-[#58a6ff] transition-colors ${mono ? "font-mono" : ""}`}
      />
      {password && (
        <button type="button" onClick={() => setShow(s => !s)}
          className="absolute right-2.5 top-1/2 -translate-y-1/2 text-[#484f58] hover:text-[#8b949e]">
          {show ? <EyeOff size={13} /> : <Eye size={13} />}
        </button>
      )}
    </div>
  );
}

function Toggle({ checked, onChange }: { checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <button type="button" onClick={() => onChange(!checked)}
      className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors ${checked ? "bg-[#238636]" : "bg-[#30363d]"}`}>
      <span className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform ${checked ? "translate-x-4" : "translate-x-0.5"}`} />
    </button>
  );
}

function FieldRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[150px_1fr] items-start gap-3">
      <label className="font-mono text-[14px] text-[#8b949e] text-right pt-2">{label}</label>
      <div>{children}</div>
    </div>
  );
}

// ── Encryption Key management ───────────────────────────────────────────────
//
// Three actions, deliberately not symmetric:
//   - View/copy: always safe, read-only.
//   - Rotate: system generates a new random key and re-encrypts existing
//     data under it automatically. No typing, no data loss, single
//     lightweight confirm — this should be the default way to "change
//     the key," not a destructive one.
//   - Paste a key: covers both restoring a previously backed-up key and
//     resetting after losing one, unified into a single flow. We try the
//     pasted key against the *current* config.json first — if it
//     decrypts, this was a legitimate restore and nothing is lost. Only
//     if it fails do we ask for the typed "NO RECOVERY" confirmation,
//     because that's the only path that actually destroys data.
function EncryptionKeySection() {
  const [key, setKey]         = useState<string | null>(null);
  const [reveal, setReveal]   = useState(false);
  const [copied, setCopied]   = useState(false);
  const [busy, setBusy]       = useState(false);
  const [msg, setMsg]         = useState<{ ok: boolean; text: string } | null>(null);
  const [rotateFlash, setRotateFlash] = useState(false);
  const [setKeyFlash, setSetKeyFlash] = useState(false);

  const [showRestore, setShowRestore]           = useState(false);
  const [pasteKey, setPasteKey]                 = useState("");
  const [pasteKeyConfirm, setPasteKeyConfirm]   = useState("");
  const [needsForce, setNeedsForce]             = useState(false);
  const [noRecoveryText, setNoRecoveryText]     = useState("");

  useEffect(() => {
    fetch("./api/security/key").then(r => r.json()).then(d => setKey(d.key)).catch(() => {});
  }, []);

  const copyKey = async () => {
    if (!key) return;
    const ok = await copyToClipboard(key);
    if (ok) {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } else {
      setMsg({ ok: false, text: "Copy failed — reveal the key and select/copy it manually" });
    }
  };

  const rotate = async () => {
    if (!window.confirm(
      "Rotate the encryption key?\n\nA new random key is generated and every stored device " +
      "credential is automatically re-encrypted under it. Nothing is lost."
    )) return;
    setBusy(true); setMsg(null);
    try {
      const r = await fetch("./api/security/rotate-key", { method: "POST" });
      const d = await r.json();
      if (r.ok) {
        setKey(d.key);
        setMsg({ ok: true, text: "Key rotated — all credentials re-encrypted." });
        setRotateFlash(true);
        setTimeout(() => setRotateFlash(false), 900);
      }
      else setMsg({ ok: false, text: d.detail ?? "Rotation failed" });
    } catch { setMsg({ ok: false, text: "Rotation failed" }); }
    finally { setBusy(false); }
  };

  const resetRestoreForm = () => {
    setShowRestore(false); setPasteKey(""); setPasteKeyConfirm("");
    setNeedsForce(false); setNoRecoveryText("");
  };

  const submitPastedKey = async (force: boolean) => {
    setBusy(true); setMsg(null);
    try {
      const r = await fetch("./api/security/set-key", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: pasteKey, force }),
      });
      const d = await r.json();
      if (r.ok) {
        setKey(pasteKey);
        setMsg({ ok: true, text: force
          ? "Key replaced — previous credentials are unrecoverable, config reset to empty."
          : `Key restored — ${d.devices} device(s) recovered.` });
        setSetKeyFlash(true);
        // Let the green flash actually register before the form collapses.
        setTimeout(() => { setSetKeyFlash(false); resetRestoreForm(); }, 700);
      } else if (r.status === 409 && !force) {
        // Pasted key doesn't decrypt the existing file — this is now the
        // destructive path. Require the typed confirmation before retrying.
        setNeedsForce(true);
      } else {
        setMsg({ ok: false, text: d.detail ?? "Failed to set key" });
      }
    } catch { setMsg({ ok: false, text: "Failed to set key" }); }
    finally { setBusy(false); }
  };

  const handleRestoreSubmit = () => {
    if (!pasteKey) return;
    if (pasteKey !== pasteKeyConfirm) { setMsg({ ok: false, text: "The two entries don't match" }); return; }
    if (needsForce) {
      if (noRecoveryText !== "NO RECOVERY") return;
      submitPastedKey(true);
    } else {
      submitPastedKey(false);
    }
  };

  return (
    <div className="px-4 py-3 border-t border-[#21262d]">
      <p className="font-mono text-[13px] uppercase tracking-widest text-[#484f58] mb-2 flex items-center gap-1.5">
        <KeyRound size={12} /> Encryption Key
      </p>
      <p className="font-mono text-[11px] text-[#484f58] mb-2 leading-relaxed">
        Encrypts every stored device password, API key, and username at rest.
        Back this up somewhere safe — without it, stored credentials cannot be recovered.
      </p>

      <div className="flex items-center gap-1.5 mb-2">
        <input readOnly value={reveal ? (key ?? "loading…") : "•".repeat(24)}
          className="flex-1 min-w-0 bg-[#010409] border border-[#30363d] rounded px-3 py-2 font-mono text-[13px] text-[#e6edf3] truncate" />
        <button onClick={() => setReveal(r => !r)} title={reveal ? "Hide key" : "Reveal key"}
          className="p-2 rounded border border-[#30363d] text-[#484f58] hover:text-[#8b949e] shrink-0">
          {reveal ? <EyeOff size={13} /> : <Eye size={13} />}
        </button>
        <button onClick={copyKey} title="Copy to clipboard" disabled={!key}
          className={`p-2 rounded border transition-all duration-500 disabled:opacity-40 shrink-0 ${
            copied ? FLASH_GREEN : "border-[#30363d] text-[#484f58] hover:text-[#8b949e]"
          }`}>
          {copied ? <CheckCircle2 size={13} /> : <Copy size={13} />}
        </button>
      </div>

      <div className="flex gap-2 mb-2">
        <button onClick={rotate} disabled={busy}
          className={`flex-1 flex items-center justify-center gap-1.5 py-1.5 rounded border font-mono text-[13px] transition-all duration-500 disabled:opacity-40 ${
            rotateFlash ? FLASH_GREEN : "border-[#30363d] text-[#8b949e] hover:text-[#c9d1d9] hover:border-[#8b949e]"
          }`}>
          <RefreshCw size={11} className={busy ? "animate-spin" : ""} /> Rotate
        </button>
        <button onClick={() => setShowRestore(s => !s)} disabled={busy}
          className="flex-1 flex items-center justify-center gap-1.5 py-1.5 rounded border border-[#30363d] font-mono text-[13px] text-[#8b949e] hover:text-[#c9d1d9] hover:border-[#8b949e] disabled:opacity-40 transition-colors">
          <Upload size={11} /> Restore / Set Key
        </button>
      </div>

      {msg && (
        <p className={`font-mono text-[12px] mb-2 ${msg.ok ? "text-[#39d353]" : "text-[#f85149]"}`}>{msg.text}</p>
      )}

      {showRestore && (
        <div className="mt-1 p-3 rounded border border-[#30363d] bg-[#010409] flex flex-col gap-2">
          <p className="font-mono text-[11px] text-[#484f58]">
            Paste a previously backed-up key. If it matches your current data, it's restored with
            nothing lost. If it doesn't match, this becomes a destructive reset.
          </p>
          <TextInput value={pasteKey} onChange={v => { setPasteKey(v); setNeedsForce(false); }}
            placeholder="Paste key" mono password />
          <TextInput value={pasteKeyConfirm} onChange={setPasteKeyConfirm}
            placeholder="Paste key again to confirm" mono password />

          {needsForce && (
            <div className="p-2 rounded border border-[#f85149]/40 bg-[#f85149]/10 flex flex-col gap-1.5">
              <p className="font-mono text-[12px] text-[#f85149] leading-relaxed">
                This key does NOT match your current stored credentials. Continuing will permanently
                discard every device password, API key, and username on file. This cannot be undone.
              </p>
              <p className="font-mono text-[11px] text-[#8b949e]">
                Type <span className="text-[#e6edf3] font-semibold">NO RECOVERY</span> to confirm:
              </p>
              <TextInput value={noRecoveryText} onChange={setNoRecoveryText} mono placeholder="NO RECOVERY" />
            </div>
          )}

          <div className="flex gap-2">
            <button
              onClick={handleRestoreSubmit}
              disabled={busy || !pasteKey || pasteKey !== pasteKeyConfirm || (needsForce && noRecoveryText !== "NO RECOVERY")}
              className={`flex-1 flex items-center justify-center gap-1.5 py-1.5 rounded border font-mono text-[13px] transition-all duration-500 disabled:opacity-40 ${
                setKeyFlash
                  ? FLASH_GREEN
                  : needsForce
                    ? "border-[#f85149]/60 bg-[#f85149]/20 text-[#f85149] hover:bg-[#f85149]/30"
                    : "border-[#238636]/60 bg-[#238636]/20 text-[#39d353] hover:bg-[#238636]/30"
              }`}
            >
              {setKeyFlash ? <CheckCircle2 size={13} /> : null}
              {setKeyFlash ? "Saved" : needsForce ? "Replace key — data will be lost" : "Set Key"}
            </button>
            <button onClick={resetRestoreForm}
              className="px-3 py-1.5 rounded border border-[#30363d] font-mono text-[13px] text-[#8b949e] hover:text-[#c9d1d9]">
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Settings Panel ────────────────────────────────────────────────────────────

function SettingsPanel({
  onClose, bgColor, onBgColor, certPath, keyPath, onSavePaths,
  autoDeployOnRenewal, onAutoDeployToggle,
  notifyEnabled, onNotifyToggle, pollIntervalMs, onPollIntervalChange,
  appVersion,
}: {
  onClose: () => void;
  bgColor: string; onBgColor: (c: string) => void;
  certPath: string; keyPath: string;
  onSavePaths: (cert: string, key: string) => Promise<boolean>;
  autoDeployOnRenewal: boolean; onAutoDeployToggle: (v: boolean) => Promise<boolean>;
  notifyEnabled: boolean; onNotifyToggle: (v: boolean) => Promise<boolean>;
  pollIntervalMs: number; onPollIntervalChange: (ms: number) => Promise<boolean>;
  appVersion: string | null;
}) {
  const [localCert, setLocalCert] = useState(certPath);
  const [localKey,  setLocalKey]  = useState(keyPath);
  const [savingPaths, setSavingPaths]   = useState<"idle" | "ok" | "error">("idle");
  const [savingPoll, setSavingPoll]     = useState<"idle" | "ok" | "error">("idle");
  const ref = useRef<HTMLDivElement>(null);

  const doSavePaths = async () => {
    setSavingPaths("idle");
    const ok = await onSavePaths(localCert, localKey);
    setSavingPaths(ok ? "ok" : "error");
    setTimeout(() => setSavingPaths("idle"), ok ? 900 : 2000);
  };

  const doPollIntervalChange = async (ms: number) => {
    setSavingPoll("idle");
    const ok = await onPollIntervalChange(ms);
    setSavingPoll(ok ? "ok" : "error");
    setTimeout(() => setSavingPoll("idle"), ok ? 900 : 2000);
  };

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [onClose]);

  return (
    <div ref={ref} className="absolute right-0 top-full mt-2 w-80 max-h-[85vh] overflow-y-auto bg-[#161b22] border border-[#30363d] rounded-lg shadow-2xl z-50">
      <div className="flex items-center justify-between px-4 py-3 border-b border-[#21262d]">
        <span className="flex items-baseline gap-2">
          <span className="font-mono text-[15px] font-semibold text-[#e6edf3]">Settings</span>
          <span className="font-mono text-[12px] font-semibold text-[#39d353] tracking-widest">
            v{appVersion ?? APP_VERSION_FALLBACK}
          </span>
        </span>
        <button onClick={onClose} className="text-[#484f58] hover:text-[#8b949e]"><X size={14} /></button>
      </div>

      {/* Background color */}
      <div className="px-4 py-3 border-b border-[#21262d]">
        <p className="font-mono text-[13px] uppercase tracking-widest text-[#484f58] mb-2">Background Color</p>
        <div className="flex flex-wrap gap-2 mb-2">
          {BG_PRESETS.map(p => (
            <button key={p.value} onClick={() => onBgColor(p.value)}
              title={p.label}
              style={{ backgroundColor: p.value }}
              className={`w-7 h-7 rounded border-2 transition-all ${bgColor === p.value ? "border-[#58a6ff] scale-110" : "border-[#30363d] hover:border-[#8b949e]"}`}
            />
          ))}
        </div>
        <div className="flex items-center gap-2">
          <input type="color" value={bgColor} onChange={e => onBgColor(e.target.value)}
            className="w-8 h-8 rounded border border-[#30363d] bg-transparent cursor-pointer" />
          <span className="font-mono text-[14px] text-[#484f58]">{bgColor}</span>
        </div>
      </div>

      {/* Certificate paths */}
      <div className="px-4 py-3">
        <p className="font-mono text-[13px] uppercase tracking-widest text-[#484f58] mb-2">Certificate Files</p>
        <div className="flex flex-col gap-2 mb-3">
          <div>
            <label className="font-mono text-[13px] text-[#8b949e] block mb-1">Cert / fullchain</label>
            <TextInput value={localCert} onChange={setLocalCert} placeholder={DEFAULT_CERT_PATH} mono />
          </div>
          <div>
            <label className="font-mono text-[13px] text-[#8b949e] block mb-1">Private key</label>
            <TextInput value={localKey} onChange={setLocalKey} placeholder={DEFAULT_KEY_PATH} mono />
          </div>
        </div>
        <button
          onClick={doSavePaths}
          className={`w-full flex items-center justify-center gap-1.5 py-1.5 rounded border font-mono text-[14px] transition-all duration-500 ${
            savingPaths === "ok" ? FLASH_GREEN
              : savingPaths === "error" ? "border-[#f85149]/60 bg-[#f85149]/20 text-[#f85149]"
              : "border-[#238636]/60 bg-[#238636]/20 text-[#39d353] hover:bg-[#238636]/30"
          }`}
        >
          {savingPaths === "ok" ? <CheckCircle2 size={12} /> : savingPaths === "error" ? <AlertCircle size={12} /> : <Save size={12} />}
          {savingPaths === "ok" ? "Saved" : savingPaths === "error" ? "Save failed" : "Save Paths"}
        </button>
      </div>

      {/* Auto-deploy on renewal */}
      <div className="px-4 py-3 border-t border-[#21262d]">
        <p className="font-mono text-[13px] uppercase tracking-widest text-[#484f58] mb-2">Automation</p>
        <label className="flex items-center justify-between gap-3 cursor-pointer group">
          <div>
            <p className="font-mono text-[13px] text-[#e6edf3]">Auto-deploy on renewal</p>
            <p className="font-mono text-[11px] text-[#484f58] mt-0.5">Deploy all devices when the cert serial changes. Runs server-side, so it works even with this dashboard closed. Automatically paused if a Let's Encrypt staging/test certificate is detected.</p>
          </div>
          <button
            onClick={() => onAutoDeployToggle(!autoDeployOnRenewal)}
            className={`relative flex-shrink-0 w-10 h-5 rounded-full transition-colors ${autoDeployOnRenewal ? "bg-[#238636]" : "bg-[#30363d]"}`}
          >
            <span className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white transition-transform ${autoDeployOnRenewal ? "translate-x-5" : "translate-x-0"}`} />
          </button>
        </label>
      </div>

      {/* Polling interval */}
      <div className="px-4 py-3 border-t border-[#21262d]">
        <p className="font-mono text-[13px] uppercase tracking-widest text-[#484f58] mb-2 flex items-center justify-between">
          <span>Polling Interval</span>
          {savingPoll === "ok" && <span className="text-[#39d353] normal-case tracking-normal flex items-center gap-1"><CheckCircle2 size={11} /> Saved</span>}
          {savingPoll === "error" && <span className="text-[#f85149] normal-case tracking-normal flex items-center gap-1"><AlertCircle size={11} /> Save failed</span>}
        </p>
        <p className="font-mono text-[11px] text-[#484f58] mb-2">
          How often the dashboard re-checks the local cert and device status.
        </p>
        <div className="relative">
          <select value={pollIntervalMs} onChange={e => doPollIntervalChange(parseInt(e.target.value))}
            className={`w-full appearance-none bg-[#010409] border rounded px-3 py-2 font-mono text-[14px] text-[#e6edf3] focus:outline-none focus:border-[#58a6ff] transition-all duration-500 pr-8 ${
              savingPoll === "ok" ? "border-[#39d353]" : savingPoll === "error" ? "border-[#f85149]" : "border-[#30363d]"
            }`}>
            {POLL_INTERVALS.map(p => (
              <option key={p.value} value={p.value}>{p.label}</option>
            ))}
          </select>
          <ChevronDown size={13} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-[#484f58] pointer-events-none" />
        </div>
      </div>

      {/* HA notifications */}
      <div className="px-4 py-3 border-t border-[#21262d]">
        <p className="font-mono text-[13px] uppercase tracking-widest text-[#484f58] mb-2 flex items-center gap-1.5">
          <Bell size={12} /> Notifications
        </p>
        <label className="flex items-center justify-between gap-3 cursor-pointer group">
          <div>
            <p className="font-mono text-[13px] text-[#e6edf3]">Home Assistant notifications</p>
            <p className="font-mono text-[11px] text-[#484f58] mt-0.5">
              Notify on auto-triggered deploy results and persistent cert-read failures
            </p>
          </div>
          <Toggle checked={notifyEnabled} onChange={onNotifyToggle} />
        </label>
      </div>

      <EncryptionKeySection />
    </div>
  );
}

// ── Device Modal ──────────────────────────────────────────────────────────────

const EMPTY_DEVICE: DeviceConfigEntry = {
  id: "", name: "", type: "truenas", enabled: true, host: "",
  port: undefined, username: "", password: "", api_key: "", site_id: "",
  pki_domain: "", ssl_policy: "", startup_config_path: "",
  verify_tls: true, pfsense_allow_upload: false, proxmox_allow_upload: false, omadac_id: "", p12_password: "",
  delete_old_certs: true,
};

function DeviceModal({
  initial, onSave, onDelete, onClose,
}: {
  initial?: DeviceConfigEntry;
  onSave: (d: DeviceConfigEntry) => void;
  onDelete?: () => void;
  onClose: () => void;
}) {
  const [dev, setDev]               = useState<DeviceConfigEntry>(initial ?? EMPTY_DEVICE);
  const [connectResult, setConnectResult] = useState<ConnectResult | null>(null);
  const [connecting, setConnecting] = useState(false);
  const [deleteConfirm, setDeleteConfirm] = useState(false);
  const [hostError, setHostError]   = useState<string | null>(null);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const isNew = !initial;

  const set = (key: keyof DeviceConfigEntry, val: unknown) =>
    setDev(prev => ({ ...prev, [key]: val }));

  const fields = TYPE_FIELDS[dev.type] ?? [];

  const testConnection = async () => {
    setConnecting(true);
    setConnectResult(null);
    const typeInfo = DEVICE_TYPE_MAP[dev.type];
    const port = dev.port || typeInfo?.defaultPort || 443;
    try {
      const r = await fetch("./api/verify-host", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ host: dev.host, port }),
      });
      setConnectResult(await r.json());
    } catch {
      setConnectResult({ ok: false, message: "Request failed", latency_ms: -1 });
    } finally {
      setConnecting(false);
    }
  };

  const handleSave = () => {
    if (!dev.name.trim()) return;
    const err = validateHost(dev.host);
    if (err) { setHostError(err); return; }
    setHostError(null);
    const saved = { ...dev };
    if (!saved.id) saved.id = slugify(dev.name) || String(Date.now());
    onSave(saved);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm">
      <div className="w-full max-w-lg bg-[#0d1117] border border-[#30363d] rounded-lg shadow-2xl overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-[#21262d]">
          <span className="font-mono text-[16px] font-semibold text-[#e6edf3]">
            {isNew ? "Add Device" : `Edit — ${initial?.name}`}
          </span>
          <button onClick={onClose} className="text-[#484f58] hover:text-[#c9d1d9]"><X size={16} /></button>
        </div>

        {/* Body */}
        <div className="px-5 py-4 flex flex-col gap-3 max-h-[70vh] overflow-y-auto">
          {/* Type selector */}
          <FieldRow label="Device type">
            <div className="relative">
              <select value={dev.type} onChange={e => set("type", e.target.value as DeviceType)}
                className="w-full appearance-none bg-[#010409] border border-[#30363d] rounded px-3 py-2 font-mono text-[15px] text-[#e6edf3] focus:outline-none focus:border-[#58a6ff] transition-colors pr-8">
                {DEVICE_TYPES.map(dt => (
                  <option key={dt.value} value={dt.value}>{dt.icon}  {dt.label}</option>
                ))}
              </select>
              <ChevronDown size={13} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-[#484f58] pointer-events-none" />
            </div>
          </FieldRow>

          <div className="border-t border-[#21262d]" />

          <FieldRow label="Display name">
            <TextInput value={dev.name} onChange={v => { set("name", v); if (isNew) set("id", slugify(v)); }}
              placeholder={DEVICE_TYPE_MAP[dev.type]?.namePlaceholder ?? "My Device"} />
          </FieldRow>

          <FieldRow label="Hostname / IP">
            <div className="flex flex-col gap-1.5">
              <TextInput value={dev.host} onChange={v => { set("host", v); setConnectResult(null); setHostError(null); }}
                placeholder="192.168.1.10 or device.example.com" mono />
              {hostError && (
                <span className="font-mono text-[13px] text-[#f85149] flex items-center gap-1">
                  <AlertCircle size={11} /> {hostError}
                </span>
              )}
              {/* Test connection */}
              <div className="flex items-center gap-2">
                <button onClick={testConnection} disabled={!dev.host.trim() || connecting}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded border border-[#30363d] font-mono text-[14px] text-[#8b949e] hover:text-[#c9d1d9] hover:border-[#8b949e] disabled:opacity-40 disabled:cursor-not-allowed transition-colors">
                  {connecting ? <Loader2 size={11} className="animate-spin" /> : <Zap size={11} />}
                  Test Connection
                </button>
                {connectResult && (
                  <span className={`font-mono text-[14px] ${connectResult.ok ? "text-[#39d353]" : "text-[#f85149]"}`}>
                    {connectResult.ok ? "✓" : "✗"} {connectResult.message}
                  </span>
                )}
              </div>
            </div>
          </FieldRow>

          <FieldRow label="Enabled">
            <div className="flex items-center gap-2 pt-1">
              <Toggle checked={dev.enabled} onChange={v => set("enabled", v)} />
              <span className="font-mono text-[14px] text-[#484f58]">{dev.enabled ? "Active" : "Disabled"}</span>
            </div>
          </FieldRow>

          {fields.length > 0 && <div className="border-t border-[#21262d]" />}

          {fields.includes("username") && (
            <FieldRow label={dev.type === "proxmox" ? "Token ID" : "Username"}>
              <TextInput value={dev.username ?? ""} onChange={v => set("username", v)} mono={dev.type === "proxmox"}
                placeholder={dev.type === "proxmox" ? "root@pam!CertFleetAuth" : "admin"} />
              {dev.type === "proxmox" && (
                <p className="font-mono text-[11px] text-[#484f58] mt-1">
                  Full token ID from Datacenter → Permissions → API Tokens (user@realm!tokenname).
                </p>
              )}
            </FieldRow>
          )}
          {fields.includes("password") && (
            <FieldRow label="Password">
              <TextInput value={dev.password ?? ""} onChange={v => set("password", v)} password />
            </FieldRow>
          )}
          {fields.includes("api_key") && (
            <FieldRow label={dev.type === "comware" ? "XTD CLI password" : dev.type === "proxmox" ? "Token secret" : "API key"}>
              <TextInput value={dev.api_key ?? ""} onChange={v => set("api_key", v)} password
                placeholder={dev.type === "comware" ? "foes-bent-pile-atom-ship" : dev.type === "truenas" ? "TrueNAS API key" : dev.type === "proxmox" ? "Token secret (UUID)" : "API key"} />
              {dev.type === "comware" && (
                <p className="font-mono text-[11px] text-[#484f58] mt-1">
                  Default XTD-mode password: <span className="text-[#8b949e]">foes-bent-pile-atom-ship</span>
                </p>
              )}
            </FieldRow>
          )}
          {fields.includes("port") && (
            <FieldRow label="Port (optional)">
              <TextInput value={dev.port?.toString() ?? ""} mono
                onChange={v => set("port", v ? parseInt(v) : undefined)}
                placeholder={String(DEVICE_TYPE_MAP[dev.type]?.defaultPort ?? 443)} />
            </FieldRow>
          )}
          {fields.includes("site_id") && (
            <FieldRow label={dev.type === "proxmox" ? "Node name" : "Site name"}>
              <TextInput value={dev.site_id ?? ""} onChange={v => set("site_id", v)} mono
                placeholder={dev.type === "proxmox" ? "proxmoxdemo" : "Default"} />
              {dev.type === "proxmox" && (
                <p className="font-mono text-[11px] text-[#484f58] mt-1">
                  The short node name as registered in the cluster — not the FQDN. Find it under
                  Datacenter in the Proxmox web UI.
                </p>
              )}
            </FieldRow>
          )}
          {fields.includes("omadac_id") && (
            <FieldRow label="Omada ID (opt)">
              <TextInput value={dev.omadac_id ?? ""} onChange={v => set("omadac_id", v)} mono
                placeholder="32-char hex — auto-discovered if blank" />
            </FieldRow>
          )}
          {/* Comware advanced settings — collapsed by default */}
          {dev.type === "comware" && (
            <div className="mt-1">
              <button
                type="button"
                onClick={() => setShowAdvanced(v => !v)}
                className="flex items-center gap-1.5 font-mono text-[12px] text-[#484f58] hover:text-[#8b949e] transition-colors"
              >
                <ChevronDown size={12} className={`transition-transform ${showAdvanced ? "rotate-180" : ""}`} />
                Advanced switch settings
              </button>
              {showAdvanced && (
                <div className="mt-2 pl-3 border-l border-[#21262d] flex flex-col gap-2">
                  <p className="font-mono text-[11px] text-[#484f58]">
                    Leave blank to use defaults. Change only if your switch uses non-standard Comware config names.
                  </p>
                  <FieldRow label="PKI domain">
                    <TextInput value={dev.pki_domain ?? ""} onChange={v => set("pki_domain", v)}
                      placeholder="hp-1950" />
                  </FieldRow>
                  <FieldRow label="SSL policy">
                    <TextInput value={dev.ssl_policy ?? ""} onChange={v => set("ssl_policy", v)}
                      placeholder="hp-1950" />
                  </FieldRow>
                  <FieldRow label="Startup config">
                    <TextInput value={dev.startup_config_path ?? ""} mono
                      onChange={v => set("startup_config_path", v)}
                      placeholder="flash:/startup.cfg" />
                    <p className="font-mono text-[11px] text-[#484f58] mt-1">
                      Run <span className="text-[#8b949e]">display startup</span> on the switch to confirm the filename.
                    </p>
                  </FieldRow>
                </div>
              )}
            </div>
          )}
          {fields.includes("ssl_policy") && dev.type !== "comware" && (
            <FieldRow label="SSL policy">
              <TextInput value={dev.ssl_policy ?? ""} onChange={v => set("ssl_policy", v)}
                placeholder="acme" />
            </FieldRow>
          )}
          {fields.includes("pki_domain") && dev.type !== "comware" && (
            <FieldRow label="PKI domain">
              <TextInput value={dev.pki_domain ?? ""} onChange={v => set("pki_domain", v)}
                placeholder="*.example.com" />
            </FieldRow>
          )}
          {fields.includes("verify_tls") && (
            <FieldRow label="Verify TLS">
              <div className="pt-1"><Toggle checked={dev.verify_tls ?? true} onChange={v => set("verify_tls", v)} /></div>
            </FieldRow>
          )}
          {fields.includes("pfsense_allow_upload") && (
            <FieldRow label="Allow upload">
              <div className="flex items-center gap-2 pt-1">
                <Toggle checked={dev.pfsense_allow_upload ?? false} onChange={v => set("pfsense_allow_upload", v)} />
                <span className="font-mono text-[14px] text-[#484f58]">
                  {dev.pfsense_allow_upload ? "Upload enabled" : "Verify-only (ACME manages renewal)"}
                </span>
              </div>
            </FieldRow>
          )}
          {fields.includes("proxmox_allow_upload") && (
            <FieldRow label="Allow upload">
              <div className="flex items-center gap-2 pt-1">
                <Toggle checked={dev.proxmox_allow_upload ?? false} onChange={v => set("proxmox_allow_upload", v)} />
                <span className="font-mono text-[14px] text-[#484f58]">
                  {dev.proxmox_allow_upload ? "Upload enabled" : "Verify-only (Proxmox's own ACME client manages renewal)"}
                </span>
              </div>
            </FieldRow>
          )}

          <div className="border-t border-[#21262d]" />
          <FieldRow label="Device ID">
            <span className="font-mono text-[14px] text-[#30363d] pt-1 block">{dev.id || "(auto)"}</span>
          </FieldRow>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between px-5 py-4 border-t border-[#21262d]">
          {/* Delete */}
          <div>
            {!isNew && onDelete && (
              deleteConfirm ? (
                <div className="flex items-center gap-2">
                  <span className="font-mono text-[14px] text-[#f85149]">Delete this device?</span>
                  <button onClick={onDelete}
                    className="px-3 py-1.5 rounded border border-[#f85149]/40 bg-[#f85149]/10 font-mono text-[14px] text-[#f85149] hover:bg-[#f85149]/20 transition-colors">
                    Yes, delete
                  </button>
                  <button onClick={() => setDeleteConfirm(false)}
                    className="font-mono text-[14px] text-[#484f58] hover:text-[#8b949e] transition-colors">
                    Cancel
                  </button>
                </div>
              ) : (
                <button onClick={() => setDeleteConfirm(true)}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded border border-[#30363d] font-mono text-[14px] text-[#484f58] hover:text-[#f85149] hover:border-[#f85149]/40 transition-colors">
                  <Trash2 size={12} /> Delete
                </button>
              )
            )}
          </div>

          <div className="flex items-center gap-2">
            <button onClick={onClose}
              className="px-3 py-1.5 rounded border border-[#30363d] font-mono text-[14px] text-[#8b949e] hover:text-[#c9d1d9] hover:border-[#8b949e] transition-colors">
              Cancel
            </button>
            <button onClick={handleSave} disabled={!dev.name.trim() || !dev.host.trim()}
              className="flex items-center gap-1.5 px-4 py-1.5 rounded border border-[#238636]/60 bg-[#238636]/20 font-mono text-[14px] text-[#39d353] hover:bg-[#238636]/30 disabled:opacity-40 disabled:cursor-not-allowed transition-colors">
              <Save size={12} />
              {isNew ? "Add Device" : "Save Changes"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Device Card ───────────────────────────────────────────────────────────────

function DeviceCard({ device, localFp, onCheck, onDeploy, onBackup, onEdit, backupState }: {
  device: Device; localFp: string | null;
  onCheck: () => void; onDeploy: () => void;
  onBackup?: () => void; onEdit: () => void;
  backupState?: BackupState;
}) {
  const inSync    = !!(device.live_fingerprint && localFp && device.live_fingerprint === localFp);
  const hasFp     = !!(device.live_fingerprint && localFp);
  const isPfsense = device.type === "pfsense";
  const isProxmox = device.type === "proxmox";
  const isOmada   = device.type === "omada";
  const typeInfo  = DEVICE_TYPE_MAP[device.type];
  // pfSense and Proxmox both can defer cert renewal to their own built-in
  // ACME client — the upload gate/label logic is identical for both,
  // just with device-specific wording.
  const hasUploadGate = isPfsense || isProxmox;
  const uploadAllowed = isPfsense ? device.pfsense_allow_upload : isProxmox ? device.proxmox_allow_upload : undefined;
  const acmeLabel = isPfsense ? "ACME handles renewal" : "Proxmox's own ACME client handles renewal";

  const cardBorder = device.running
    ? "border-[#1f6feb]/40"
    : device.last_warning
      ? "border-[#e3b341]/50"
      : "border-[#21262d]";

  return (
    <div className={`rounded border bg-card p-4 flex flex-col gap-3 transition-all ${cardBorder}`}>
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-lg leading-none">{typeInfo?.icon ?? "📦"}</span>
          <div className="min-w-0">
            <p className="text-[16px] font-semibold text-[#e6edf3] truncate">{device.name}</p>
            <p className="font-mono text-[14px] text-[#8b949e] truncate">{device.host}</p>
          </div>
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          <button onClick={onEdit} className="p-1 rounded text-[#30363d] hover:text-[#8b949e] transition-colors" title="Edit device">
            <Edit2 size={12} />
          </button>
          {device.running ? (
            <Pill label="Running…" color="bg-[#1f6feb]/10 border-[#1f6feb]/30 text-[#1f6feb]" />
          ) : device.last_status ? (
            <Pill label={statusLabel(device.last_status)} color={`${statusBg(device.last_status)} ${statusColor(device.last_status)}`} />
          ) : (
            <Pill label="Not checked" color="bg-[#30363d]/40 border-[#30363d] text-[#8b949e]" />
          )}
        </div>
      </div>

      {device.last_warning && (
        <div className="rounded border border-[#e3b341]/40 bg-[#e3b341]/10 px-3 py-2 text-[13px] font-mono text-[#e3b341]">
          <div className="flex items-center gap-1.5 mb-1">
            <AlertCircle size={11} />
            <span className="text-[14px]">Heads up</span>
          </div>
          <div className="text-[13px] opacity-90 leading-relaxed">
            {device.last_warning}
          </div>
        </div>
      )}

      {hasFp && (
        <div className={`rounded border px-3 py-2 text-[13px] font-mono ${inSync ? "border-[#39d353]/20 bg-[#39d353]/5 text-[#39d353]" : "border-[#e3b341]/20 bg-[#e3b341]/5 text-[#e3b341]"}`}>
          <div className="flex items-center gap-1.5 mb-1">
            {inSync ? <CheckCircle2 size={11} /> : <AlertCircle size={11} />}
            <span className="text-[14px]">{inSync ? "Fingerprint match" : "Fingerprint mismatch"}</span>
          </div>
          <div className="text-[13px] opacity-80 leading-relaxed" title={device.live_fingerprint ?? undefined}>
            {shortFp(device.live_fingerprint ?? "")}
          </div>
        </div>
      )}
      {!hasFp && device.live_fingerprint && device.last_status === "no_local_cert" && (
        <div className="rounded border border-[#58a6ff]/20 bg-[#58a6ff]/5 px-3 py-2 text-[13px] font-mono text-[#58a6ff]">
          <div className="flex items-center gap-1.5 mb-1">
            <Shield size={11} />
            <span className="text-[14px]">Device's current certificate</span>
          </div>
          <div className="text-[13px] opacity-80 leading-relaxed" title={device.live_fingerprint}>
            {shortFp(device.live_fingerprint)}
          </div>
        </div>
      )}

      {device.last_message && (
        <p className={`text-[14px] leading-5 ${statusColor(device.last_status)}`}>{device.last_message}</p>
      )}
      {device.last_run && (
        <p className="text-[13px] font-mono text-[#39d353]">
          {formatLocalTime(device.last_run)}
        </p>
      )}
      {hasUploadGate && (
        <p className="text-[13px] text-[#8b949e] font-mono">
          {uploadAllowed ? "Upload enabled" : `Verify-only (${acmeLabel})`}
        </p>
      )}

      <div className="flex gap-2 mt-auto pt-1">
        <button onClick={onCheck} disabled={device.running || !device.enabled}
          className="flex-1 flex items-center justify-center gap-1.5 py-2 rounded border border-[#30363d] bg-[#161b27] text-[14px] font-mono text-[#8b949e] hover:text-[#c9d1d9] hover:border-[#8b949e] disabled:opacity-40 disabled:cursor-not-allowed transition-colors">
          {device.running ? <Loader2 size={12} className="animate-spin" /> : <Activity size={12} />}
          Verify
        </button>
        <button onClick={onDeploy}
          disabled={device.running || !device.enabled || (hasUploadGate && !uploadAllowed)}
          title={hasUploadGate && !uploadAllowed ? "Allow upload must be enabled in the device editor" : undefined}
          className={`flex-1 flex items-center justify-center gap-1.5 py-2 rounded border text-[14px] font-mono disabled:opacity-40 disabled:cursor-not-allowed transition-colors ${hasUploadGate && !uploadAllowed ? "border-[#30363d] bg-transparent text-[#30363d]" : "border-[#39d353]/30 bg-[#39d353]/10 text-[#39d353] hover:bg-[#39d353]/20"}`}>
          {device.running ? <Loader2 size={12} className="animate-spin" /> : <Upload size={12} />}
          {hasUploadGate && !uploadAllowed ? "ACME" : "Deploy"}
        </button>
      </div>

      {isOmada && (
        <div className="flex gap-2 pt-1 border-t border-[#21262d]">
          <button onClick={onBackup}
            disabled={device.running || !device.enabled || backupState?.status === "running"}
            className="flex-1 flex items-center justify-center gap-1.5 py-2 rounded border border-[#6e40c9]/30 bg-[#6e40c9]/10 text-[14px] font-mono text-[#a371f7] hover:bg-[#6e40c9]/20 disabled:opacity-40 disabled:cursor-not-allowed transition-colors">
            {backupState?.status === "running" ? <Loader2 size={12} className="animate-spin" /> : <HardDrive size={12} />}
            {backupState?.status === "running" ? "Backing up…" : "Backup Config"}
          </button>
          {backupState?.status === "done" && backupState.filename && (
            <a href={`./api/devices/${device.id}/backup/latest`} download={backupState.filename}
              className="flex items-center justify-center gap-1.5 px-3 py-2 rounded border border-[#6e40c9]/30 bg-[#6e40c9]/10 text-[14px] font-mono text-[#a371f7] hover:bg-[#6e40c9]/20 transition-colors"
              title={`Download ${backupState.filename}`}>
              <Download size={12} />
            </a>
          )}
          {backupState?.status === "error" && (
            <span className="flex items-center text-[13px] font-mono text-[#f85149]" title={backupState.error}>
              <AlertCircle size={11} className="mr-1" /> Failed
            </span>
          )}
        </div>
      )}
    </div>
  );
}

// ── Main App ──────────────────────────────────────────────────────────────────

export default function App() {
  const [cert, setCert]             = useState<LocalCert | null>(null);
  const [certError, setCertError]   = useState<string | null>(null);
  const [devices, setDevices]       = useState<Device[]>([]);
  const [logs, setLogs]             = useState<LogEntry[]>([]);
  // The backend replays its whole log buffer on every SSE (re)connect —
  // which happens on its own after any network blip, not just page reloads —
  // so "clear" has to remember a boundary id and keep filtering the replay,
  // not just wipe local state once.
  const clearedBeforeId = useRef(0);
  const [polling, setPolling]       = useState(true);
  const [deployingAll, setDeployingAll]   = useState(false);
  const [verifyingAll, setVerifyingAll]   = useState(false);
  const [backupStates, setBackupStates] = useState<Record<string, BackupState>>({});

  // Config editor
  const [configDevices, setConfigDevices] = useState<DeviceConfigEntry[]>([]);
  const [showModal, setShowModal]         = useState(false);
  const [editingDevice, setEditingDevice] = useState<DeviceConfigEntry | undefined>();

  // Settings
  const [showSettings, setShowSettings] = useState(false);
  const [bgColor, setBgColor]           = useState(() => localStorage.getItem("ha-cert-bg") || "#0d1117");
  const [certPath, setCertPath]         = useState(DEFAULT_CERT_PATH);
  const [keyPath,  setKeyPath]          = useState(DEFAULT_KEY_PATH);
  // Persisted server-side (auto_deploy_on_renewal in /api/config) rather than
  // localStorage — the backend poll loop is what actually acts on this
  // setting now, and it has no access to the browser's localStorage.
  const [autoDeployOnRenewal, setAutoDeployOnRenewal] = useState(false);
  const [notifyEnabled,   setNotifyEnabled]   = useState(true);
  const [pollIntervalMs,  setPollIntervalMs]  = useState(DEFAULT_POLL_INTERVAL_MS);
  const [updateAvailable, setUpdateAvailable] = useState(false);
  const [latestVersion,   setLatestVersion]   = useState<string | null>(null);
  const [appVersion,      setAppVersion]      = useState<string | null>(null);
  const [configError,     setConfigError]     = useState<string | null>(null);
  const settingsRef = useRef<HTMLDivElement>(null);

  // Refresh button flash
  const [certFlash, setCertFlash] = useState(false);
  // Pulses on every poll tick (success or failure) so "polling" isn't just a static label
  const [pollTickFlash, setPollTickFlash] = useState(false);

  const handleBgColor = (c: string) => {
    setBgColor(c);
    localStorage.setItem("ha-cert-bg", c);
  };

  const fetchCert = useCallback(() => {
    fetch("./api/cert")
      .then(r => r.ok ? r.json() : r.json().then((e: {detail?: string}) => Promise.reject(e.detail ?? "Error")))
      .then(data => { setCert(data); setCertError(null); })
      .catch(e => { setCert(null); setCertError(String(e)); });
  }, []);

  const handleRefreshCert = useCallback(() => {
    fetchCert();
    setCertFlash(true);
    setTimeout(() => setCertFlash(false), 900);
  }, [fetchCert]);

  const fetchDevices = useCallback(() => {
    fetch("./api/devices")
      .then(r => r.ok ? r.json() : r.json().then((e: {detail?: string}) => Promise.reject(e.detail ?? r.statusText)))
      .then(data => { setDevices(data); setConfigError(null); })
      .catch(e => { if (String(e).includes("decrypt")) setConfigError(String(e)); });
  }, []);

  const fetchConfig = useCallback(() => {
    fetch("./api/config")
      .then(r => r.ok ? r.json() : r.json().then((e: {detail?: string}) => Promise.reject(e.detail ?? r.statusText)))
      .then((data: AppConfig) => {
        setConfigDevices(data.devices ?? []);
        if (data.cert_path) setCertPath(data.cert_path);
        if (data.key_path)  setKeyPath(data.key_path);
        setNotifyEnabled(data.notify_enabled ?? true);
        setPollIntervalMs(data.poll_interval_ms ?? DEFAULT_POLL_INTERVAL_MS);
        setAutoDeployOnRenewal(data.auto_deploy_on_renewal ?? false);
        setConfigError(null);
      })
      .catch(e => { if (String(e).includes("decrypt")) setConfigError(String(e)); });
  }, []);

  // fetch() only rejects on a network failure — a 4xx/5xx from the backend
  // resolves normally, so every caller here explicitly checks res.ok and
  // returns real success/failure instead of optimistically assuming it
  // worked (this was the root cause of Settings controls claiming "saved"
  // with no way to tell if the write actually landed).
  const saveConfig = useCallback(async (updates: Partial<AppConfig>): Promise<boolean> => {
    try {
      const currentCfg = await fetch("./api/config").then(r => r.ok ? r.json() : Promise.reject());
      const merged = { ...currentCfg, ...updates };
      const res = await fetch("./api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(merged),
      });
      if (!res.ok) return false;
      fetchDevices();
      return true;
    } catch {
      return false;
    }
  }, [fetchDevices]);

  const handleNotifyToggle = useCallback(async (v: boolean): Promise<boolean> => {
    setNotifyEnabled(v);
    const ok = await saveConfig({ notify_enabled: v });
    if (!ok) setNotifyEnabled(!v); // roll back the optimistic UI update on a real failure
    return ok;
  }, [saveConfig]);

  const handleAutoDeployToggle = useCallback(async (v: boolean): Promise<boolean> => {
    setAutoDeployOnRenewal(v);
    const ok = await saveConfig({ auto_deploy_on_renewal: v });
    if (!ok) setAutoDeployOnRenewal(!v);
    return ok;
  }, [saveConfig]);

  const handlePollIntervalChange = useCallback(async (ms: number): Promise<boolean> => {
    const prev = pollIntervalMs;
    setPollIntervalMs(ms);
    const ok = await saveConfig({ poll_interval_ms: ms });
    if (!ok) setPollIntervalMs(prev);
    return ok;
  }, [saveConfig, pollIntervalMs]);

  const normalizeSslPath = (p: string, def: string) => {
    const v = p.trim() || def;
    return v.startsWith("/") ? v : `/ssl/${v}`;
  };

  const handleSavePaths = useCallback(async (cert: string, key: string): Promise<boolean> => {
    const c = normalizeSslPath(cert, DEFAULT_CERT_PATH);
    const k = normalizeSslPath(key,  DEFAULT_KEY_PATH);
    const ok = await saveConfig({ cert_path: c, key_path: k });
    if (ok) {
      setCertPath(c);
      setKeyPath(k);
      fetchCert();
    }
    return ok;
  }, [saveConfig, fetchCert]);

  // SSE
  useEffect(() => {
    const es = new EventSource("./api/events");
    es.onmessage = (e) => {
      const entry: LogEntry = JSON.parse(e.data);
      if (entry.id <= clearedBeforeId.current) return;
      setLogs(prev => prev.some(e => e.id === entry.id) ? prev : [entry, ...prev.slice(0, 199)]);
      fetchDevices();
    };
    es.onerror = () => {};
    return () => es.close();
  }, [fetchDevices]);

  useEffect(() => { fetchCert(); fetchDevices(); fetchConfig(); }, [fetchCert, fetchDevices, fetchConfig]);

  useEffect(() => {
    fetch("./api/supervisor/addon-info")
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        if (d?.update_available) { setUpdateAvailable(true); setLatestVersion(d.version_latest ?? null); }
        if (d?.version) setAppVersion(d.version);
      })
      .catch(() => null);
  }, []);

  // Display refresh only — renewal detection, auto-deploy, cert-unreadable
  // tracking, and staging-cert handling all run server-side now (main.py's
  // poll loop), independent of whether this tab is even open. This effect
  // just keeps what's on screen current.
  useEffect(() => {
    if (!polling) return;
    const id = setInterval(async () => {
      // Fires on every tick regardless of outcome — this is the only
      // visible confirmation that the polling loop is actually alive
      // and doing something, not just showing a static "polling" label.
      setPollTickFlash(true);
      setTimeout(() => setPollTickFlash(false), 700);
      const res = await fetch("./api/cert").catch(() => null);
      if (res?.ok) setCert(await res.json());
      fetchDevices();
    }, pollIntervalMs);
    return () => clearInterval(id);
  }, [polling, fetchDevices, pollIntervalMs]);

  const handleAddDevice = useCallback((d: DeviceConfigEntry) => {
    setShowModal(false);
    setEditingDevice(undefined);
    const updated = [...configDevices, d];
    setConfigDevices(updated);
    saveConfig({ devices: updated });
  }, [configDevices, saveConfig]);

  const handleEditDevice = useCallback((d: DeviceConfigEntry) => {
    setShowModal(false);
    setEditingDevice(undefined);
    const updated = configDevices.map(e => e.id === d.id ? d : e);
    setConfigDevices(updated);
    saveConfig({ devices: updated });
  }, [configDevices, saveConfig]);

  const handleDeleteDevice = useCallback((id: string) => {
    setShowModal(false);
    setEditingDevice(undefined);
    const updated = configDevices.filter(d => d.id !== id);
    setConfigDevices(updated);
    saveConfig({ devices: updated });
  }, [configDevices, saveConfig]);

  const callDevice = useCallback(async (id: string, action: "check" | "deploy") => {
    if (action === "deploy" && cert?.is_staging && !window.confirm(
      "The certificate currently loaded is a Let's Encrypt STAGING (untrusted, test) certificate, "
      + "not a real one.\n\nDeploy it to this device anyway?"
    )) return;
    setDevices(prev => prev.map(d => d.id === id ? { ...d, running: true } : d));
    try { await fetch(`./api/devices/${id}/${action}`, { method: "POST" }); }
    catch (e) { console.error(e); }
    fetchDevices();
  }, [fetchDevices, cert]);

  const callBackup = useCallback(async (id: string) => {
    setBackupStates(prev => ({ ...prev, [id]: { status: "running" } }));
    try {
      const r = await fetch(`./api/devices/${id}/backup`, { method: "POST" });
      if (!r.ok) throw new Error((await r.json()).detail ?? r.statusText);
      const data = await r.json();
      setBackupStates(prev => ({ ...prev, [id]: { status: "done", filename: data.filename } }));
    } catch (e) {
      setBackupStates(prev => ({ ...prev, [id]: { status: "error", error: String(e) } }));
    }
  }, []);

  const deployAll = useCallback(async () => {
    if (cert?.is_staging && !window.confirm(
      "The certificate currently loaded is a Let's Encrypt STAGING (untrusted, test) certificate, "
      + "not a real one. Deploying it will push an untrusted certificate to every enabled device.\n\n"
      + "Deploy anyway?"
    )) return;
    setDeployingAll(true);
    try { await fetch("./api/devices/deploy-all", { method: "POST" }); }
    catch (e) { console.error(e); }
    await fetchDevices();
    setDeployingAll(false);
  }, [fetchDevices, cert]);

  const verifyAll = useCallback(async () => {
    setVerifyingAll(true);
    try { await fetch("./api/devices/check-all", { method: "POST" }); }
    catch (e) { console.error(e); }
    await fetchDevices();
    setVerifyingAll(false);
  }, [fetchDevices]);

  const configById = Object.fromEntries(configDevices.map(d => [d.id, d]));
  const certDays    = cert?.days_remaining ?? 0;
  const isStaging   = cert?.is_staging ?? false;
  // A staging cert that WOULD have been auto-deployed is the dangerous
  // combination — that's a hard red, not just an informational yellow.
  const stagingDangerous = isStaging && autoDeployOnRenewal;
  const certStatus  = certDays <= 14 || stagingDangerous ? "error"
    : isStaging || certDays <= 30 ? "warn"
    : "ok";
  const localFp     = cert?.fingerprint ?? null;
  const syncedCount = devices.filter(d =>
    d.last_status === "already_current" ||
    (d.last_status === "deployed" && localFp && d.live_fingerprint === localFp)
  ).length;
  const errorCount  = devices.filter(d => d.last_status === "error").length;

  return (
    <div className="min-h-screen text-foreground transition-colors duration-300"
      style={{ backgroundColor: bgColor, fontFamily: "'Inter', sans-serif" }}>

      {showModal && (
        <DeviceModal
          initial={editingDevice}
          onSave={editingDevice ? handleEditDevice : handleAddDevice}
          onDelete={editingDevice ? () => handleDeleteDevice(editingDevice.id) : undefined}
          onClose={() => { setShowModal(false); setEditingDevice(undefined); }}
        />
      )}

      {/* Header */}
      <header className="border-b border-[#21262d] bg-[#0a0e14]/95 backdrop-blur sticky top-0 z-10">
        <div className="max-w-[1600px] mx-auto px-6 h-12 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <Shield size={15} className="text-[#39d353]" />
            <span className="font-mono text-[16px] font-semibold text-[#e6edf3]">CertFleet</span>
            <span className="hidden sm:inline text-[#30363d] text-lg">|</span>
            <span className="hidden sm:inline font-mono text-[14px] text-[#8b949e]">Let's Encrypt → All Devices</span>
          </div>
          <div className="flex items-center gap-4">
            <div className="flex items-center gap-2">
              <span className="font-mono text-[12px] text-[#484f58] tracking-widest">v{appVersion ?? APP_VERSION_FALLBACK}</span>
              {updateAvailable && (
                <a
                  href="/hassio/addon/certfleet/info"
                  target="_parent"
                  title={latestVersion ? `v${latestVersion} available` : "Update available"}
                  className="flex items-center gap-1 px-1.5 py-0.5 rounded text-[11px] font-mono font-semibold bg-[#9e6a03]/20 border border-[#9e6a03]/40 text-[#e3b341] hover:bg-[#9e6a03]/30 transition-colors"
                >
                  <Zap size={10} />
                  {latestVersion ? `v${latestVersion}` : "Update"}
                </a>
              )}
            </div>
            {devices.length > 0 && (
              <div className="hidden md:flex items-center gap-3 font-mono text-[14px]">
                <span className="text-[#39d353]">{syncedCount}/{devices.length} in sync</span>
                {errorCount > 0 && <span className="text-[#f85149]">{errorCount} error{errorCount > 1 ? "s" : ""}</span>}
              </div>
            )}
            <button onClick={() => setPolling(p => !p)}
              title={polling ? "Click to pause polling" : "Click to resume polling"}
              className={`flex items-center gap-1.5 text-[14px] font-mono transition-colors ${polling ? "text-[#39d353] hover:text-[#39d353]/70" : "text-[#8b949e] hover:text-[#c9d1d9]"}`}>
              {polling
                ? <Wifi size={13} className={`transition-transform duration-300 ${pollTickFlash ? "scale-125" : "scale-100"}`} />
                : <WifiOff size={13} />}
              {polling ? (certError ? "waiting" : "polling") : "paused"}
            </button>
            {/* Settings gear */}
            <div className="relative" ref={settingsRef}>
              <button onClick={() => setShowSettings(s => !s)}
                className={`p-1.5 rounded border transition-colors ${showSettings ? "border-[#58a6ff]/40 text-[#58a6ff]" : "border-[#30363d] text-[#484f58] hover:text-[#8b949e] hover:border-[#8b949e]"}`}>
                <Settings size={14} />
              </button>
              {showSettings && (
                <SettingsPanel
                  onClose={() => setShowSettings(false)}
                  bgColor={bgColor} onBgColor={handleBgColor}
                  certPath={certPath} keyPath={keyPath}
                  onSavePaths={handleSavePaths}
                  autoDeployOnRenewal={autoDeployOnRenewal}
                  onAutoDeployToggle={handleAutoDeployToggle}
                  notifyEnabled={notifyEnabled} onNotifyToggle={handleNotifyToggle}
                  pollIntervalMs={pollIntervalMs} onPollIntervalChange={handlePollIntervalChange}
                  appVersion={appVersion}
                />
              )}
            </div>
          </div>
        </div>
      </header>

      <main className="max-w-[1600px] mx-auto px-6 py-6 flex flex-col gap-6">

        {configError && (
          <section className="rounded border border-[#f85149]/30 bg-[#f85149]/10 p-4 flex items-start gap-3">
            <FileWarning size={18} className="text-[#f85149] shrink-0 mt-0.5" />
            <div>
              <p className="text-[15px] font-semibold text-[#f85149] mb-1">Device configuration is unreadable</p>
              <p className="font-mono text-[13px] text-[#c9d1d9] leading-relaxed">{configError}</p>
              <p className="font-mono text-[13px] text-[#484f58] mt-1">
                Open <button onClick={() => setShowSettings(true)} className="text-[#58a6ff] hover:underline">Settings → Encryption Key</button> to restore or reset the key.
              </p>
            </div>
          </section>
        )}

        {/* ── Cert Banner ─────────────────────────────────────────────── */}
        <section className="rounded border border-[#21262d] bg-card p-5">
          {certError ? (
            devices.length === 0 ? (
              /* No cert + no devices: soft placeholder — Getting Started below has the details */
              <div className="flex items-center gap-3 text-[#484f58]">
                <FolderOpen size={18} />
                <span className="font-mono text-[14px]">No certificate loaded — follow the steps below to get started.</span>
                <button onClick={handleRefreshCert} title="Check again"
                  className={`ml-auto p-1.5 rounded border transition-all duration-500 ${certFlash ? "border-[#39d353] bg-[#39d353]/20 text-[#39d353]" : "border-[#30363d] text-[#484f58] hover:text-[#c9d1d9] hover:border-[#8b949e]"}`}>
                  <RefreshCw size={13} className={certFlash ? "animate-spin" : ""} />
                </button>
              </div>
            ) : (
              /* No cert but devices exist: show the actionable error */
              <div className="flex items-start gap-3">
                <div className="p-2 rounded border border-[#f85149]/30 bg-[#f85149]/10 shrink-0">
                  <FileWarning size={20} className="text-[#f85149]" />
                </div>
                <div className="flex-1">
                  <p className="text-[16px] font-semibold text-[#f85149] mb-1">Certificate file error</p>
                  <p className="font-mono text-[14px] text-[#c9d1d9] whitespace-pre-wrap leading-relaxed">{certError}</p>
                  <p className="font-mono text-[14px] text-[#484f58] mt-2">
                    Check the cert paths in <button onClick={() => setShowSettings(true)} className="text-[#58a6ff] hover:underline">Settings</button>.
                  </p>
                </div>
                <button onClick={handleRefreshCert} title="Refresh Local Cert Info"
                  className={`p-1.5 rounded border transition-all duration-500 shrink-0 ${certFlash ? "border-[#39d353] bg-[#39d353]/20 text-[#39d353]" : "border-[#30363d] text-[#484f58] hover:text-[#c9d1d9] hover:border-[#8b949e]"}`}>
                  <RefreshCw size={13} className={certFlash ? "animate-spin" : ""} />
                </button>
              </div>
            )
          ) : (
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
              <div className="flex items-start gap-4">
                <div className={`p-2 rounded border ${certStatus === "error" ? "border-[#f85149]/30 bg-[#f85149]/10" : certStatus === "warn" ? "border-[#e3b341]/30 bg-[#e3b341]/10" : "border-[#39d353]/30 bg-[#39d353]/10"}`}>
                  <Shield size={20} className={certStatus === "error" ? "text-[#f85149]" : certStatus === "warn" ? "text-[#e3b341]" : "text-[#39d353]"} />
                </div>
                <div>
                  <div className="flex items-center gap-3 mb-0.5 flex-wrap">
                    <span className="text-[17px] font-semibold text-[#e6edf3]">{cert?.domain ?? "Loading…"}</span>
                    {cert && (
                      <span className={`font-mono text-[14px] font-medium ${certDayColor(certDays)}`}>
                        {certDays} days remaining
                      </span>
                    )}
                  </div>
                  {cert && (
                    <>
                      <div className="flex flex-wrap items-center gap-x-4 gap-y-0.5">
                        <span className={`font-mono text-[14px] ${cert.is_staging ? "text-[#e3b341]" : "text-[#8b949e]"}`}>Issuer: {cert.issuer}</span>
                        <span className="font-mono text-[14px] text-[#8b949e]">Expires {cert.not_after.substring(0, 10)}</span>
                      </div>
                      {/* Full fingerprint */}
                      <div className="font-mono text-[13px] text-[#39d353]/70 mt-1.5 break-all leading-relaxed max-w-xl">
                        {cert.fingerprint}
                      </div>
                    </>
                  )}
                </div>
              </div>

              <div className="flex flex-col items-end gap-1.5 shrink-0">
                <div className="flex items-center gap-2">
                  <button onClick={handleRefreshCert}
                    title="Refresh Local Cert Info"
                    className={`p-1.5 rounded border transition-all duration-500 ${certFlash ? "border-[#39d353] bg-[#39d353]/20 text-[#39d353]" : "border-[#30363d] text-[#484f58] hover:text-[#c9d1d9] hover:border-[#8b949e]"}`}>
                    <RefreshCw size={13} className={certFlash ? "animate-spin" : ""} />
                  </button>
                  <button onClick={verifyAll} disabled={verifyingAll || deployingAll || devices.length === 0}
                    className="flex items-center gap-2 px-4 py-2 rounded border border-[#58a6ff]/40 bg-[#58a6ff]/10 text-[15px] font-mono font-medium text-[#58a6ff] hover:bg-[#58a6ff]/20 hover:border-[#58a6ff]/60 disabled:opacity-40 disabled:cursor-not-allowed transition-all">
                    {verifyingAll ? <Loader2 size={13} className="animate-spin" /> : <Activity size={13} />}
                    Verify All
                  </button>
                  <button onClick={deployAll} disabled={deployingAll || verifyingAll || devices.length === 0}
                    className="flex items-center gap-2 px-4 py-2 rounded border border-[#39d353]/40 bg-[#39d353]/15 text-[15px] font-mono font-medium text-[#39d353] hover:bg-[#39d353]/25 hover:border-[#39d353]/60 disabled:opacity-40 disabled:cursor-not-allowed transition-all">
                    {deployingAll ? <Loader2 size={13} className="animate-spin" /> : <Upload size={13} />}
                    Deploy All
                  </button>
                </div>
              </div>
            </div>
          )}

          {cert && !certError && cert.is_staging && (
            <div className={`mt-4 rounded border px-3 py-2.5 text-[13px] font-mono ${
              stagingDangerous
                ? "border-[#f85149]/40 bg-[#f85149]/10 text-[#f85149]"
                : "border-[#e3b341]/40 bg-[#e3b341]/10 text-[#e3b341]"
            }`}>
              <div className="flex items-center gap-1.5 mb-1 font-semibold">
                <AlertCircle size={13} />
                <span>Test/staging certificate detected</span>
              </div>
              <div className="opacity-90 leading-relaxed">
                A Let's Encrypt STAGING (untrusted) certificate is currently loaded at{" "}
                <span className="font-semibold">{cert.cert_path}</span>.
                {stagingDangerous
                  ? " Auto-deploy is paused until a valid certificate is issued — no device will receive this certificate automatically."
                  : " This certificate will not be trusted by browsers or devices — nothing will auto-deploy while auto-deploy stays off."}
              </div>
            </div>
          )}

          {cert && !certError && (
            <div className="mt-4 pt-4 border-t border-[#21262d]">
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-x-4 gap-y-3">
                {[
                  { label: "Valid from",  value: cert.not_before.substring(0, 10) },
                  { label: "Expires",    value: cert.not_after.substring(0, 10) },
                  { label: "Key",        value: cert.key_info },
                  { label: "Algorithm",  value: cert.sig_algorithm },
                  { label: "Root CA",    value: cert.root_ca },
                  { label: "Key usage",  value: cert.key_usage },
                  { label: "Key file",   value: cert.key_path, mono: true },
                  { label: "Cert file",  value: cert.cert_path, mono: true },
                ].map(({ label, value, mono }) => (
                  <div key={label}>
                    <p className="font-mono text-[13px] text-[#484f58] uppercase tracking-wider mb-0.5">{label}</p>
                    <p className={`text-[14px] truncate ${mono ? "font-mono" : ""} ${label === "Root CA" && cert.is_staging ? "text-[#e3b341]" : "text-[#39d353]"}`} title={value}>{value}</p>
                  </div>
                ))}
              </div>
              {cert.sans.length > 0 && (
                <div className="mt-3 pt-3 border-t border-[#21262d]">
                  <p className="font-mono text-[13px] text-[#484f58] uppercase tracking-wider mb-1">
                    Subject Alternative Names ({cert.sans.length})
                  </p>
                  <div className="flex flex-wrap gap-1.5">
                    {cert.sans.map(san => (
                      <span key={san} className="font-mono text-[13px] text-[#39d353] bg-[#39d353]/5 border border-[#39d353]/20 rounded px-2 py-0.5">
                        {san}
                      </span>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </section>

        {/* ── Devices ─────────────────────────────────────────────────── */}
        <section>
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-2">
              <span className="font-mono text-[13px] uppercase tracking-[0.15em] text-[#484f58]">Devices</span>
              <span className="font-mono text-[13px] text-[#30363d]">{devices.length}</span>
            </div>
            <button onClick={() => { setEditingDevice(undefined); setShowModal(true); }}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded border border-[#30363d] font-mono text-[14px] text-[#8b949e] hover:text-[#c9d1d9] hover:border-[#8b949e] transition-colors">
              <Plus size={12} /> Add Device
            </button>
          </div>

          {devices.length === 0 ? (
            <div className="rounded border border-[#21262d] bg-card p-6 flex flex-col gap-5">
              <div className="flex items-center gap-2">
                <Shield size={15} className="text-[#39d353]" />
                <span className="font-mono text-[15px] font-semibold text-[#e6edf3]">Getting Started</span>
              </div>

              {/* Step 1 — cert */}
              <div className="flex gap-3">
                <div className={`mt-0.5 shrink-0 w-5 h-5 rounded-full flex items-center justify-center text-[11px] font-bold ${cert ? "bg-[#39d353]/15 text-[#39d353]" : "bg-[#f85149]/15 text-[#f85149]"}`}>
                  {cert ? "✓" : "!"}
                </div>
                <div>
                  <p className="font-mono text-[14px] text-[#e6edf3] mb-0.5">{cert ? "Certificate detected" : "Install Let's Encrypt"}</p>
                  {cert
                    ? <p className="font-mono text-[13px] text-[#8b949e]">{cert.domain} — {cert.days_remaining} days remaining</p>
                    : <p className="font-mono text-[13px] text-[#f85149]">No cert found at <span className="text-[#e6edf3]">/ssl/fullchain.pem</span>. Ensure Let's Encrypt is configured in Home Assistant.</p>
                  }
                </div>
              </div>

              {/* Step 2 — add devices */}
              <div className="flex gap-3">
                <div className="mt-0.5 shrink-0 w-5 h-5 rounded-full flex items-center justify-center text-[11px] font-bold bg-[#1f6feb]/15 text-[#1f6feb]">2</div>
                <div>
                  <p className="font-mono text-[14px] text-[#e6edf3] mb-0.5">Add your devices</p>
                  <p className="font-mono text-[13px] text-[#8b949e] mb-2">Click <span className="text-[#e6edf3]">+ Add Device</span> above for each device that needs your certificate. Supported types:</p>
                  <div className="flex flex-wrap gap-2">
                    {DEVICE_TYPES.map(dt => (
                      <span key={dt.value} className="font-mono text-[13px] px-2 py-0.5 rounded border border-[#30363d] text-[#8b949e]">{dt.icon} {dt.label}</span>
                    ))}
                  </div>
                </div>
              </div>

              {/* Step 3 — verify & deploy */}
              <div className="flex gap-3">
                <div className="mt-0.5 shrink-0 w-5 h-5 rounded-full flex items-center justify-center text-[11px] font-bold bg-[#1f6feb]/15 text-[#1f6feb]">3</div>
                <div>
                  <p className="font-mono text-[14px] text-[#e6edf3] mb-0.5">Verify, then Deploy</p>
                  <p className="font-mono text-[13px] text-[#8b949e]">Use <span className="text-[#e6edf3]">Verify</span> to check if each device already has your certificate, then <span className="text-[#e6edf3]">Deploy</span> to push it.</p>
                </div>
              </div>


              <button onClick={() => { setEditingDevice(undefined); setShowModal(true); }}
                className="self-start inline-flex items-center gap-2 px-4 py-2 rounded border border-[#1f6feb]/40 bg-[#1f6feb]/10 font-mono text-[15px] text-[#58a6ff] hover:bg-[#1f6feb]/20 transition-colors">
                <Plus size={13} /> Add your first device
              </button>
            </div>
          ) : (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
              {devices.map(dev => (
                <DeviceCard key={dev.id} device={dev} localFp={cert?.fingerprint ?? null}
                  onCheck={() => callDevice(dev.id, "check")}
                  onDeploy={() => callDevice(dev.id, "deploy")}
                  onBackup={dev.type === "omada" ? () => callBackup(dev.id) : undefined}
                  onEdit={() => { setEditingDevice(configById[dev.id] ?? { ...EMPTY_DEVICE, ...dev }); setShowModal(true); }}
                  backupState={backupStates[dev.id]}
                />
              ))}
              <button onClick={() => { setEditingDevice(undefined); setShowModal(true); }}
                className="rounded border border-dashed border-[#21262d] p-4 flex flex-col items-center justify-center gap-2 text-[#30363d] hover:text-[#8b949e] hover:border-[#30363d] transition-colors min-h-[160px]">
                <Plus size={22} />
                <span className="font-mono text-[14px]">Add Device</span>
              </button>
            </div>
          )}
        </section>

        {/* ── Event Log ───────────────────────────────────────────────── */}
        <section className="rounded border border-[#21262d] bg-card p-5">
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-2">
              <Terminal size={14} className="text-[#8b949e]" />
              <span className="font-mono text-[13px] uppercase tracking-[0.15em] text-[#8b949e]">Event Log</span>
            </div>
            <div className="flex items-center gap-3">
              <span className="font-mono text-[14px] text-[#484f58]">{logs.length} entries</span>
              <button
                onClick={() => {
                  const maxId = logs.reduce((m, l) => Math.max(m, l.id), 0);
                  clearedBeforeId.current = maxId;
                  setLogs([{
                    id: maxId,
                    ts: new Date().toISOString(),
                    level: "info",
                    msg: "Event log cleared",
                    device: null,
                  }]);
                }}
                className="font-mono text-[14px] text-[#30363d] hover:text-[#8b949e] transition-colors"
              >clear</button>
            </div>
          </div>
          <div className="overflow-y-auto space-y-px" style={{ maxHeight: "320px", scrollbarWidth: "thin", scrollbarColor: "#30363d transparent" }}>
            {logs.length === 0 ? (
              <p className="text-[14px] text-[#30363d] font-mono py-2">Waiting for events…</p>
            ) : logs.map(entry => (
              <div key={entry.id} className="flex gap-3 py-1.5 border-b border-[#21262d]/60 last:border-0">
                <span className="font-mono text-[13px] text-[#484f58] shrink-0 pt-px whitespace-nowrap">{formatLocalTime(entry.ts)}</span>
                <span className={`font-mono text-[13px] uppercase w-14 shrink-0 pt-px ${logLevelColor(entry.level)}`}>{entry.level}</span>
                {entry.device && (
                  <span className="font-mono text-[13px] text-[#8b949e] shrink-0 pt-px whitespace-nowrap">[{entry.device}]</span>
                )}
                <span className="text-[15px] text-[#c9d1d9] leading-5">{entry.msg}</span>
              </div>
            ))}
          </div>
        </section>
      </main>
    </div>
  );
}
