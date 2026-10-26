/**
 * OpenJarvis WhatsApp Baileys Bridge
 *
 * JSON-line protocol on stdio:
 *
 * Input commands (stdin):
 *   {"type":"send","jid":"<jid>","text":"<message>","request_id":"<id>"?}
 *   {"type":"logout"}       -- unlink the device, wipe auth, emit a new QR
 *   {"type":"pairing_code","phone":"<digits>"}
 *                           -- link by typing a code on the phone instead of
 *                              scanning the QR (for when the page is opened
 *                              on that same phone)
 *   {"type":"disconnect"}
 *
 * Output events (stdout):
 *   {"type":"message","jid":"<jid>","sender":"<sender>","text":"<text>","message_id":"<id>"}
 *   {"type":"status","status":"connected"|"disconnected"|"logged_out","me":"<number>"}
 *   {"type":"qr","data":"<qr-string>","svg":"<svg markup>"}
 *   {"type":"error","message":"<description>"}
 *   {"type":"inbox","chat_jid":..,"sender_jid":..,"phone":..,"push_name":..,
 *    "text":..,"message_id":..,"ts":..,"is_group":..,"from_me":..}
 *        -- any other chat's message (stored/notified, never answered)
 *   {"type":"contact","jid":..,"phone":..,"name":..,"push_name":..}
 *   {"type":"send_result","request_id":..,"ok":bool,"jid":..,"error":..}
 *        -- only for sends that carried a request_id
 *   {"type":"pairing_code","phone":..,"code":"ABCD1234","error":..}
 *
 * Arguments:
 *   --auth-dir <dir>      Baileys multi-file auth state (persist this!)
 *   --mode self|own       self: personal number, only the "Message yourself"
 *                         chat talks to the assistant (default).
 *                         own: the assistant has its own number; only numbers
 *                         listed in --allowed may talk to it.
 *   --allowed <n1,n2>     Comma-separated phone numbers (digits, with country
 *                         code) allowed in "own" mode.
 */

import makeWASocket, {
  areJidsSameUser,
  DisconnectReason,
  fetchLatestBaileysVersion,
  generateMessageIDV2,
  useMultiFileAuthState,
  WASocket,
} from "@whiskeysockets/baileys";
import * as fs from "fs";
import * as readline from "readline";
/* eslint-disable @typescript-eslint/no-var-requires */
const QRCode = require("qrcode");
const pino = require("pino");
/* eslint-enable @typescript-eslint/no-var-requires */

// Baileys logs through pino; keep stdout clean for the JSON-line protocol.
const logger = pino({ level: "warn" }, pino.destination(2));

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function emit(event: Record<string, unknown>): void {
  process.stdout.write(JSON.stringify(event) + "\n");
}

interface Args {
  authDir: string;
  mode: "self" | "own";
  allowed: Set<string>;
}

function digits(value: string): string {
  return (value || "").replace(/\D/g, "");
}

function parseArgs(): Args {
  const args = process.argv.slice(2);
  const out: Args = { authDir: "./auth", mode: "self", allowed: new Set() };
  for (let i = 0; i < args.length; i++) {
    const next = i + 1 < args.length ? args[i + 1] : "";
    if (args[i] === "--auth-dir" && next) {
      out.authDir = next;
      i++;
    } else if (args[i] === "--mode" && next) {
      out.mode = next === "own" ? "own" : "self";
      i++;
    } else if (args[i] === "--allowed") {
      for (const n of next.split(",")) {
        const d = digits(n);
        if (d) out.allowed.add(d);
      }
      i++;
    }
  }
  return out;
}

/** Phone number (digits) of a JID, or "" for LID / group / other JIDs. */
function phoneOf(jid: string | null | undefined): string {
  if (!jid || !jid.endsWith("@s.whatsapp.net")) return "";
  return digits(jid.split("@")[0].split(":")[0]);
}

function extractText(message: any): string {
  return (
    message?.conversation ||
    message?.extendedTextMessage?.text ||
    message?.ephemeralMessage?.message?.extendedTextMessage?.text ||
    message?.ephemeralMessage?.message?.conversation ||
    ""
  );
}

/** Text, or a short placeholder (with caption) for media messages. */
function describeMessage(message: any): string {
  const m = message?.ephemeralMessage?.message || message?.viewOnceMessage?.message || message;
  const text = extractText(m);
  if (text) return text;
  const media: Array<[string, string]> = [
    ["imageMessage", "[imagen]"],
    ["videoMessage", "[vídeo]"],
    ["audioMessage", "[audio]"],
    ["documentMessage", "[documento]"],
    ["stickerMessage", "[sticker]"],
    ["contactMessage", "[contacto]"],
    ["locationMessage", "[ubicación]"],
  ];
  for (const [field, label] of media) {
    if (m?.[field]) {
      const caption = m[field].caption || m[field].fileName || "";
      return caption ? `${label} ${caption}` : label;
    }
  }
  return "";
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  const { authDir, mode, allowed } = parseArgs();

  // IDs of messages this bridge sent. In "self" mode our own replies land in
  // the same chat as the user's messages, so they must not be fed back in.
  const sentIds = new Set<string>();
  const rememberSent = (id: string) => {
    sentIds.add(id);
    if (sentIds.size > 500) {
      const first = sentIds.values().next().value;
      if (first) sentIds.delete(first);
    }
  };

  let sock: WASocket | null = null;
  let warnedEmptyAllowlist = false;

  function isOwnChat(jid: string): boolean {
    const me: any = sock?.user;
    if (!me || !jid) return false;
    return (
      (!!me.id && areJidsSameUser(jid, me.id)) ||
      (!!me.lid && areJidsSameUser(jid, me.lid))
    );
  }

  function senderPhones(key: any): string[] {
    return [
      key.remoteJid,
      key.remoteJidAlt,
      key.participant,
      key.participantAlt,
      key.senderPn,
    ]
      .map(phoneOf)
      .filter((p) => !!p);
  }

  function shouldHandle(msg: any): boolean {
    const key = msg.key || {};
    const jid: string = key.remoteJid || "";
    if (!jid || jid === "status@broadcast" || jid.endsWith("@g.us")) return false;
    if (key.id && sentIds.has(key.id)) return false;

    if (mode === "self") {
      // Only the "Message yourself" chat, written by the account owner.
      return !!key.fromMe && isOwnChat(jid);
    }

    // "own" mode: dedicated assistant number, allowlisted senders only.
    if (key.fromMe) return false;
    if (allowed.size === 0) {
      if (!warnedEmptyAllowlist) {
        warnedEmptyAllowlist = true;
        emit({
          type: "error",
          message:
            "mode=own but no allowed numbers configured; ignoring all messages",
        });
      }
      return false;
    }
    return senderPhones(key).some((p) => allowed.has(p));
  }

  async function startSocket(): Promise<void> {
    const { state, saveCreds } = await useMultiFileAuthState(authDir);

    // WhatsApp rejects outdated web-client versions (connection closes with
    // 405 and no QR), so ask for the current one; fall back to the bundled.
    let version: [number, number, number] | undefined;
    try {
      version = (await fetchLatestBaileysVersion()).version;
    } catch {
      version = undefined;
    }

    sock = makeWASocket({
      auth: state,
      logger,
      ...(version ? { version } : {}),
      printQRInTerminal: false,
      markOnlineOnConnect: false,
      syncFullHistory: false,
    });

    sock.ev.on("creds.update", saveCreds);

    const emitContacts = (contacts: any[]) => {
      for (const c of contacts || []) {
        const jid: string = c.id || "";
        if (!jid || jid.endsWith("@g.us")) continue;
        emit({
          type: "contact",
          jid,
          phone: phoneOf(jid) || digits(c.phoneNumber || ""),
          name: c.name || "",
          push_name: c.notify || c.verifiedName || "",
        });
      }
    };
    sock.ev.on("contacts.upsert", emitContacts);
    sock.ev.on("contacts.update", emitContacts as any);

    sock.ev.on("connection.update", async (update) => {
      const { connection, lastDisconnect, qr } = update;

      if (qr) {
        let svg = "";
        try {
          svg = await QRCode.toString(qr, { type: "svg", margin: 1 });
        } catch {
          svg = "";
        }
        emit({ type: "qr", data: qr, svg });
      }

      if (connection === "close") {
        const statusCode =
          (lastDisconnect?.error as any)?.output?.statusCode ??
          0;

        if (statusCode === DisconnectReason.loggedOut) {
          // Unlinked from the phone (or via logout): wipe the stale session
          // and start over so a fresh QR is offered.
          emit({ type: "status", status: "logged_out" });
          fs.rmSync(authDir, { recursive: true, force: true });
          setTimeout(() => void startSocket(), 1000);
        } else {
          // Transient failure (or the restart WhatsApp requests right after
          // pairing): reconnect with the saved credentials.
          emit({ type: "status", status: "disconnected" });
          setTimeout(() => void startSocket(), 2000);
        }
      } else if (connection === "open") {
        emit({
          type: "status",
          status: "connected",
          me: phoneOf((sock?.user as any)?.id || ""),
        });
      }
    });

    sock.ev.on("messages.upsert", (m) => {
      // "append" covers history sync and our own sends; only live messages.
      if (m.type !== "notify") return;
      for (const msg of m.messages) {
        if (!msg.message) continue;
        if (shouldHandle(msg)) {
          const text = extractText(msg.message);
          if (!text) continue;
          emit({
            type: "message",
            jid: msg.key.remoteJid || "",
            sender: msg.key.participant || msg.key.remoteJid || "",
            text,
            message_id: msg.key.id || "",
          });
          continue;
        }

        // Everything else is inbox: stored and maybe notified, never answered.
        const key: any = msg.key || {};
        const jid: string = key.remoteJid || "";
        if (!jid || jid === "status@broadcast" || jid.endsWith("@newsletter")) continue;
        if (key.id && sentIds.has(key.id)) continue;
        if (isOwnChat(jid)) continue;
        const text = describeMessage(msg.message);
        if (!text) continue;
        const isGroup = jid.endsWith("@g.us");
        emit({
          type: "inbox",
          chat_jid: jid,
          sender_jid: isGroup ? key.participant || "" : jid,
          phone: senderPhones(key)[0] || "",
          push_name: msg.pushName || "",
          text,
          message_id: key.id || "",
          ts: Number(msg.messageTimestamp || 0) || Date.now() / 1000,
          is_group: isGroup,
          from_me: !!key.fromMe,
        });
      }
    });
  }

  await startSocket();

  // -----------------------------------------------------------------------
  // Stdin command processing
  // -----------------------------------------------------------------------

  const rl = readline.createInterface({ input: process.stdin });

  rl.on("line", async (line: string) => {
    let cmd: Record<string, unknown>;
    try {
      cmd = JSON.parse(line);
    } catch {
      emit({ type: "error", message: "Invalid JSON on stdin" });
      return;
    }

    if (cmd.type === "send" && sock) {
      const requestId = (cmd.request_id as string) || "";
      let jid = cmd.jid as string;
      try {
        if (requestId && jid.endsWith("@s.whatsapp.net")) {
          // Explicit sends to a number: make sure it is on WhatsApp.
          const [found] = (await sock.onWhatsApp(jid)) || [];
          if (!found?.exists) {
            emit({
              type: "send_result",
              request_id: requestId,
              ok: false,
              jid,
              error: "Ese número no tiene WhatsApp",
            });
            return;
          }
          jid = found.jid || jid;
        }
        const messageId = generateMessageIDV2(sock.user?.id);
        rememberSent(messageId);
        await sock.sendMessage(jid, { text: cmd.text as string }, { messageId });
        if (requestId) {
          emit({ type: "send_result", request_id: requestId, ok: true, jid });
        }
      } catch (err: any) {
        if (requestId) {
          emit({
            type: "send_result",
            request_id: requestId,
            ok: false,
            jid,
            error: err.message,
          });
        } else {
          emit({ type: "error", message: `Send failed: ${err.message}` });
        }
      }
    } else if (cmd.type === "pairing_code") {
      const phone = digits(String(cmd.phone || ""));
      try {
        if (!sock) throw new Error("WhatsApp todavía está arrancando");
        if ((sock.authState?.creds as any)?.registered) {
          throw new Error("Este WhatsApp ya está vinculado");
        }
        if (phone.length < 8) throw new Error("Número no válido");
        const code = await sock.requestPairingCode(phone);
        emit({ type: "pairing_code", phone, code });
      } catch (err: any) {
        emit({ type: "pairing_code", phone, code: "", error: err.message });
      }
    } else if (cmd.type === "logout") {
      try {
        // Triggers connection.update(close, loggedOut) -> wipe + new QR.
        await sock?.logout();
      } catch {
        fs.rmSync(authDir, { recursive: true, force: true });
        sock?.end(undefined);
      }
    } else if (cmd.type === "disconnect") {
      if (sock) {
        sock.end(undefined);
      }
      emit({ type: "status", status: "disconnected" });
      process.exit(0);
    }
  });

  rl.on("close", () => {
    if (sock) {
      sock.end(undefined);
    }
    process.exit(0);
  });
}

main().catch((err) => {
  emit({ type: "error", message: `Fatal: ${err.message}` });
  process.exit(1);
});
