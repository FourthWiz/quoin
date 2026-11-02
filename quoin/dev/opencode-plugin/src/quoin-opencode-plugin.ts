/**
 * Quoin lifecycle plugin for OpenCode.
 *
 * Appends one JSON record per selected lifecycle event to the log named by
 * QUOIN_OPENCODE_PLUGIN_LOG, tagged with QUOIN_OPENCODE_RUN_ID. Quoin's Python
 * side owns translating that log into its runtime events; this plugin only
 * observes. It never changes a hook's output, never answers or vetoes a
 * permission request, and never touches provider settings, so loading it
 * cannot loosen any decision OpenCode makes.
 *
 * Failures are reported, not swallowed: a missing configuration or an
 * unwritable log produces one warning on stderr, in the OpenCode log and as a
 * toast in the terminal interface.
 *
 * Only the plugin function is exported, because OpenCode calls every
 * exported function of a plugin module as a plugin.
 */
import { randomBytes } from "node:crypto";
import { appendFileSync } from "node:fs";
import { isAbsolute } from "node:path";
import type { Hooks, Plugin, PluginInput } from "@opencode-ai/plugin";

const PLUGIN_VERSION = "0.1.0";
const RECORD_VERSION = 1;
const RUN_ID_RE = /^oc-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$/;
const MAX_TEXT = 500;
const MAX_PATTERNS = 20;

// Bus events worth recording. Everything else (message parts, status
// heartbeats, catalogue refreshes) is either chatty or already covered.
const RECORDED_EVENTS = new Set([
  "session.created",
  "session.idle",
  "session.compacted",
  "session.error",
  "permission.asked",
  "permission.replied",
]);

type Data = Record<string, unknown>;

type LogRecord = {
  v: number;
  plugin_version: string;
  run_id: string;
  instance: string;
  seq: number;
  ts: string;
  source: "event" | "hook";
  native_type: string;
  native_id: string | null;
  session_id: string | null;
  parent_id: string | null;
  data: Data;
};

function obj(value: unknown): Data {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? (value as Data) : {};
}

function str(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function text(value: unknown): string | null {
  const s = str(value);
  return s === null ? null : s.length > MAX_TEXT ? s.slice(0, MAX_TEXT) : s;
}

function eventData(type: string, props: Data): { data: Data; parent: string | null } {
  switch (type) {
    case "session.created": {
      const info = obj(props.info);
      return { data: { version: str(info.version) }, parent: str(info.parentID) };
    }
    case "session.error": {
      // Kept in OpenCode's own error shape so Quoin maps it exactly like a
      // headless error; the response body and headers are left out.
      const error = obj(props.error);
      const detail = obj(error.data);
      const metadata = obj(detail.metadata);
      return {
        data: {
          error: {
            name: str(error.name),
            data: {
              message: text(detail.message),
              statusCode: typeof detail.statusCode === "number" ? detail.statusCode : null,
              isRetryable: typeof detail.isRetryable === "boolean" ? detail.isRetryable : null,
              metadata: { code: str(metadata.code) },
            },
          },
        },
        parent: null,
      };
    }
    case "permission.asked": {
      const patterns = Array.isArray(props.patterns) ? props.patterns : [];
      return {
        data: {
          permission_id: str(props.id),
          permission: str(props.permission),
          patterns: patterns.slice(0, MAX_PATTERNS).map(text).filter((p) => p !== null),
          call_id: str(obj(props.tool).callID),
        },
        parent: null,
      };
    }
    case "permission.replied":
      return {
        data: {
          permission_id: str(props.requestID) ?? str(props.permissionID) ?? str(props.id),
          reply: str(props.reply) ?? str(props.response),
        },
        parent: null,
      };
    default:
      return { data: {}, parent: null };
  }
}

export const QuoinLifecycle: Plugin = async (input: PluginInput): Promise<Hooks> => {
  const warned = new Set<string>();
  const warn = (key: string, message: string): void => {
    if (warned.has(key)) return;
    warned.add(key);
    const line = "quoin plugin: " + message;
    try {
      process.stderr.write(line + "\n");
    } catch {
      // stderr closed; the other two channels still try.
    }
    const client = input.client as any;
    Promise.resolve()
      .then(() => client?.app?.log?.({ body: { service: "quoin", level: "warn", message: line } }))
      .catch(() => undefined);
    Promise.resolve()
      .then(() => client?.tui?.showToast?.({ body: { message: line, variant: "warning" } }))
      .catch(() => undefined);
  };

  const runId = process.env.QUOIN_OPENCODE_RUN_ID ?? "";
  const logPath = process.env.QUOIN_OPENCODE_PLUGIN_LOG ?? "";
  if (!RUN_ID_RE.test(runId) || !isAbsolute(logPath)) {
    warn(
      "config",
      "not recording: QUOIN_OPENCODE_RUN_ID or QUOIN_OPENCODE_PLUGIN_LOG is missing or invalid; " +
        "start OpenCode with `quoin opencode start`",
    );
    return {};
  }

  // The sequence restarts with every plugin instance (an OpenCode restart, or
  // one instance per project in a server), so records carry a random
  // instance id that keeps (instance, seq) unique within a run.
  const instance = randomBytes(4).toString("hex");
  let seq = 0;
  const write = (
    source: LogRecord["source"],
    nativeType: string,
    nativeId: string | null,
    sessionId: string | null,
    parentId: string | null,
    data: Data,
  ): void => {
    const record: LogRecord = {
      v: RECORD_VERSION,
      plugin_version: PLUGIN_VERSION,
      run_id: runId,
      instance,
      seq: seq++,
      ts: new Date().toISOString(),
      source,
      native_type: nativeType,
      native_id: nativeId,
      session_id: sessionId,
      parent_id: parentId,
      data,
    };
    try {
      appendFileSync(logPath, JSON.stringify(record) + "\n", { encoding: "utf8", mode: 0o600 });
    } catch (err) {
      warn("write", "cannot write the event log " + logPath + ": " + String(err));
    }
  };

  // Every hook body is guarded: a malformed input must never throw into
  // OpenCode, and the awaited tool hook must stay cheap.
  const guarded = (name: string, body: () => void): void => {
    try {
      body();
    } catch (err) {
      warn("hook:" + name, "skipped a " + name + " record: " + String(err));
    }
  };

  return {
    event: async (hookInput) => {
      guarded("event", () => {
        const e = obj(obj(hookInput).event);
        const type = str(e.type);
        if (type === null || !RECORDED_EVENTS.has(type)) return;
        const props = obj(e.properties);
        const { data, parent } = eventData(type, props);
        write("event", type, str(e.id), str(props.sessionID), parent, data);
      });
    },
    "tool.execute.after": async (hookInput) => {
      guarded("tool.execute.after", () => {
        const i = obj(hookInput);
        write("hook", "tool.execute.after", str(i.callID), str(i.sessionID), null, { tool: str(i.tool) });
      });
    },
    "experimental.session.compacting": async (hookInput) => {
      guarded("experimental.session.compacting", () => {
        write("hook", "experimental.session.compacting", null, str(obj(hookInput).sessionID), null, {});
      });
    },
  };
};
