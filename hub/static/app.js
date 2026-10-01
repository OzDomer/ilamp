/**
 * i_Lamp hub UI.
 *
 * Two rules keep this simple:
 *  1. The page renders only what the hub pushes ("state" messages). A tap
 *     sends a request; the UI changes when the lamp confirms, not before.
 *  2. One request per interaction, and for the slider and color picker at
 *     most one in flight at a time, newest value wins (see latestOnly).
 *
 * The types below mirror hub/messages.py. Change one, change the other.
 */
const MODE_NAMES = ["normal", "pulse", "rhythm", "rainbow", "candlelight", "alert"];
// --- the page ------------------------------------------------------------------
function $(selector) {
    const element = document.querySelector(selector);
    if (element === null)
        throw new Error(`missing element: ${selector}`);
    return element;
}
const panel = $(".panel");
const statusText = $("#status");
const lampButton = $("#lamp");
const lampLabel = $("#lampLabel");
const brightnessInput = $("#brightness");
const brightnessValue = $("#brightnessValue");
const colorInput = $("#color");
const colorValue = $("#colorValue");
const modeButtons = $("#modes");
const toast = $("#toast");
let socket = null;
let nextId = 1;
const pending = new Map(); // requests waiting for their ack or error
let retryDelay = 1000;
function connect() {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${scheme}://${location.host}/ws`);
    socket.addEventListener("open", () => {
        retryDelay = 1000;
    });
    socket.addEventListener("message", (event) => {
        handleMessage(JSON.parse(event.data));
    });
    // "close" also fires when a connection attempt fails, so this is the
    // only place reconnecting needs to happen.
    socket.addEventListener("close", () => {
        setLink("hub", "hub unreachable, retrying…");
        failAllPending("lost the connection to the hub");
        setTimeout(connect, retryDelay);
        retryDelay = Math.min(retryDelay * 2, 8000);
    });
}
function handleMessage(message) {
    switch (message.type) {
        case "state":
            render(message.connected, message.lamp);
            break;
        case "ack":
            if (message.id !== null) {
                pending.get(message.id)?.resolve();
                pending.delete(message.id);
            }
            break;
        case "error": {
            const waiter = message.id === null ? undefined : pending.get(message.id);
            if (message.id !== null)
                pending.delete(message.id);
            if (waiter)
                waiter.reject(new Error(message.message));
            else
                showToast(message.message); // an error for a message the hub couldn't even parse
            break;
        }
    }
}
/**
 * Send one request to the hub. Resolves when the lamp confirmed it,
 * rejects with the hub's reason when it didn't.
 */
function request(body) {
    return new Promise((resolve, reject) => {
        if (socket?.readyState !== WebSocket.OPEN) {
            reject(new Error("not connected to the hub"));
            return;
        }
        const id = nextId++;
        pending.set(id, { resolve, reject });
        socket.send(JSON.stringify({ id, ...body }));
    });
}
function failAllPending(reason) {
    for (const waiter of pending.values())
        waiter.reject(new Error(reason));
    pending.clear();
}
/**
 * Wrap a sender so that at most one request is in flight. While one is
 * out, only the NEWEST value is remembered; older ones are dropped. A
 * slider fires dozens of events per second and the lamp confirms one
 * command at a time, so this keeps the lamp tracking the finger without
 * a growing backlog.
 */
function latestOnly(send) {
    let inFlight = false;
    let queued = null;
    async function run(value) {
        inFlight = true;
        try {
            await send(value);
        }
        catch (err) {
            showToast(err instanceof Error ? err.message : String(err));
        }
        inFlight = false;
        if (queued !== null) {
            const next = queued;
            queued = null;
            void run(next);
        }
    }
    return (value) => {
        if (inFlight)
            queued = value;
        else
            void run(value);
    };
}
// --- rendering what the lamp reports -------------------------------------------
/** Controls the user is holding right now; a pushed state must not yank them. */
const held = new Set();
function render(connected, lamp) {
    if (!connected || lamp === null) {
        setLink("lamp", "lamp away, reconnecting…");
        panel.dataset["on"] = "false";
        lampLabel.textContent = "away";
        return;
    }
    setLink("connected", "connected");
    const hex = toHex(lamp.rgb);
    panel.style.setProperty("--lamp-color", hex);
    panel.style.setProperty("--lamp-level", (lamp.brightness / 255).toFixed(3));
    panel.dataset["on"] = String(lamp.on);
    panel.dataset["mode"] = lamp.mode;
    lampButton.setAttribute("aria-pressed", String(lamp.on));
    lampLabel.textContent = lamp.on ? lamp.mode : "off";
    if (!held.has(brightnessInput))
        brightnessInput.value = String(lamp.brightness);
    brightnessValue.textContent = String(lamp.brightness);
    if (!held.has(colorInput))
        colorInput.value = hex;
    colorValue.textContent = hex;
    for (const button of modeButtons.querySelectorAll("button")) {
        button.classList.toggle("active", button.dataset["mode"] === lamp.mode);
    }
}
function setLink(kind, text) {
    panel.dataset["link"] = kind;
    statusText.textContent = text;
}
// --- the controls --------------------------------------------------------------
function report(err) {
    showToast(err instanceof Error ? err.message : String(err));
}
lampButton.addEventListener("click", () => {
    const isOn = lampButton.getAttribute("aria-pressed") === "true";
    request({ type: "power", on: !isOn }).catch(report);
});
const sendBrightness = latestOnly((value) => request({ type: "brightness", value }));
brightnessInput.addEventListener("input", () => {
    brightnessValue.textContent = brightnessInput.value;
    sendBrightness(Number(brightnessInput.value));
});
const sendColor = latestOnly(([r, g, b]) => request({ type: "color", r, g, b }));
colorInput.addEventListener("input", () => {
    colorValue.textContent = colorInput.value;
    sendColor(fromHex(colorInput.value));
});
for (const swatch of document.querySelectorAll(".swatch[data-rgb]")) {
    swatch.addEventListener("click", () => {
        const rgb = (swatch.dataset["rgb"] ?? "").split(",").map(Number);
        if (rgb.length === 3)
            sendColor(rgb);
    });
}
// While a finger is on the slider or the picker is open, pushed states
// update the numbers but leave the control where the user put it.
brightnessInput.addEventListener("pointerdown", () => held.add(brightnessInput));
for (const type of ["pointerup", "pointercancel"]) {
    brightnessInput.addEventListener(type, () => held.delete(brightnessInput));
}
colorInput.addEventListener("focus", () => held.add(colorInput));
for (const type of ["change", "blur"]) {
    colorInput.addEventListener(type, () => held.delete(colorInput));
}
modeButtons.addEventListener("click", (event) => {
    const target = event.target;
    const mode = target.closest("button")?.dataset["mode"];
    if (isModeName(mode))
        request({ type: "mode", mode }).catch(report);
});
// --- small helpers -------------------------------------------------------------
function isModeName(value) {
    return MODE_NAMES.includes(value);
}
function toHex([r, g, b]) {
    return `#${[r, g, b].map((v) => v.toString(16).padStart(2, "0")).join("")}`;
}
function fromHex(hex) {
    return [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
}
let toastTimer = null;
function showToast(text) {
    toast.textContent = text;
    toast.classList.add("show");
    if (toastTimer !== null)
        clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove("show"), 3200);
}
connect();
export {};
