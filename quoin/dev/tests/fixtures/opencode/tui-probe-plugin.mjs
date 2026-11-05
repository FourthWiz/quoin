// A pre-compiled terminal interface plugin probe for the OpenCode binary tier.
//
// Hand-written in the shape a Solid "universal" compile emits, with `solid-js`
// and `@opentui/solid` left as external imports for the host to resolve. On
// activation it appends one JSON line to the file named by
// QUOIN_TUI_PROBE_MARKER recording what the two imports resolved to, then
// registers a `sidebar_content` slot that appends a `render` line the first
// time the host draws it.
import { appendFileSync } from "node:fs";
import * as solid from "solid-js";
import * as opentui from "@opentui/solid";

const MARKER = process.env.QUOIN_TUI_PROBE_MARKER;
const PROBE_ID = "quoin-tui-probe";

function mark(record) {
  if (!MARKER) return;
  appendFileSync(MARKER, JSON.stringify({ probe: PROBE_ID, ...record }) + "\n");
}

function shape(module, names) {
  const out = {};
  for (const name of names) out[name] = typeof module[name];
  return out;
}

let rendered = false;

function ProbePanel(props) {
  const node = opentui.createElement("text");
  opentui.insert(node, "quoin tui probe");
  if (!rendered) {
    rendered = true;
    mark({ event: "render", slot: "sidebar_content", session_id: props.session_id ?? null });
  }
  return node;
}

async function tui(api) {
  mark({
    event: "activate",
    solid_js: shape(solid, ["createSignal", "createComponent", "createMemo"]),
    opentui_solid: shape(opentui, ["createElement", "insert", "render"]),
    api: shape(api, ["slots", "state", "kv"]),
  });
  api.slots.register({
    order: 900,
    slots: {
      sidebar_content(_ctx, props) {
        return solid.createComponent(ProbePanel, {
          get session_id() {
            return props.session_id;
          },
        });
      },
    },
  });
}

export default { id: PROBE_ID, tui };
