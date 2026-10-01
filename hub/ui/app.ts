/**
 * i_Lamp hub UI.
 *
 * Two rules keep this simple:
 *  1. The page renders only what the hub pushes ("state" messages). A tap
 *     sends a request; the UI changes when the lamp confirms, not before.
 *  2. One request per interaction, and for the sliders and color picker at
 *     most one in flight at a time, newest value wins (see latestOnly).
 *
 * The types below mirror hub/messages.py. Change one, change the other.
 */

// This file is a module (the HTML loads it with type="module"), so its names
// stay local instead of colliding with browser globals like `Request`.
export {};

// --- the protocol, as types ----------------------------------------------------

type ModeName = "normal" | "pulse" | "rhythm" | "rainbow" | "candlelight" | "sleep";
const MODE_NAMES: readonly ModeName[] = ["normal", "pulse", "rhythm", "rainbow", "candlelight", "sleep"];

type Rgb = [number, number, number];

/** The RGB light. `mode` is a string because the lamp may report a value we haven't named. */
interface LampJson {
  on: boolean;
  brightness: number;
  rgb: Rgb;
  mode: string;
}

/** The white ring. temperature: 0 warm .. 255 cold (null until the lamp has said); level: brightness 0-16. */
interface SunJson {
  on: boolean;
  temperature: number | null;
  level: number;
}

type HubMessage =
  | { type: "state"; connected: boolean; lamp: LampJson | null; sun: SunJson | null }
  | { type: "ack"; id: number | null }
  | { type: "error"; id: number | null; message: string };

type HubRequest =
  | { type: "power"; on: boolean }
  | { type: "color"; r: number; g: number; b: number; brightness?: number }
  | { type: "brightness"; value: number }
  | { type: "mode"; mode: ModeName }
  | { type: "refresh" }
  | { type: "sun"; on: boolean }
  | { type: "sun_temperature"; value: number }
  | { type: "sun_level"; value: number };

// --- the page ------------------------------------------------------------------

function $<T extends Element>(selector: string): T {
  const element = document.querySelector<T>(selector);
  if (element === null) throw new Error(`missing element: ${selector}`);
  return element;
}

const panel = $<HTMLElement>(".panel");
const statusText = $<HTMLElement>("#status");
const lampButton = $<HTMLButtonElement>("#lamp");
const offButton = $<HTMLButtonElement>("#off");
const lampLabel = $<HTMLElement>("#lampLabel");
const brightnessInput = $<HTMLInputElement>("#brightness");
const brightnessValue = $<HTMLOutputElement>("#brightnessValue");
const colorInput = $<HTMLInputElement>("#color");
const colorValue = $<HTMLOutputElement>("#colorValue");
const modeButtons = $<HTMLElement>("#modes");
const sunToggle = $<HTMLButtonElement>("#sunToggle");
const sunTempInput = $<HTMLInputElement>("#sunTemp");
const sunLevelInput = $<HTMLInputElement>("#sunLevel");
const sunLevelValue = $<HTMLOutputElement>("#sunLevelValue");
const toast = $<HTMLElement>("#toast");

// --- the socket ----------------------------------------------------------------

interface Waiter {
  resolve: () => void;
  reject: (err: Error) => void;
}

let socket: WebSocket | null = null;
let nextId = 1;
const pending = new Map<number, Waiter>(); // requests waiting for their ack or error
let retryDelay = 1000;

function connect(): void {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${scheme}://${location.host}/ws`);

  socket.addEventListener("open", () => {
    retryDelay = 1000;
  });

  socket.addEventListener("message", (event: MessageEvent<string>) => {
    handleMessage(JSON.parse(event.data) as HubMessage);
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

function handleMessage(message: HubMessage): void {
  switch (message.type) {
    case "state":
      render(message.connected, message.lamp, message.sun);
      break;
    case "ack":
      if (message.id !== null) {
        pending.get(message.id)?.resolve();
        pending.delete(message.id);
      }
      break;
    case "error": {
      const waiter = message.id === null ? undefined : pending.get(message.id);
      if (message.id !== null) pending.delete(message.id);
      if (waiter) waiter.reject(new Error(message.message));
      else showToast(message.message); // an error for a message the hub couldn't even parse
      break;
    }
  }
}

/**
 * Send one request to the hub. Resolves when the lamp confirmed it,
 * rejects with the hub's reason when it didn't.
 */
function request(body: HubRequest): Promise<void> {
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

function failAllPending(reason: string): void {
  for (const waiter of pending.values()) waiter.reject(new Error(reason));
  pending.clear();
}

/**
 * Wrap a sender so that at most one request is in flight. While one is
 * out, only the NEWEST value is remembered; older ones are dropped. A
 * slider fires dozens of events per second and the lamp confirms one
 * command at a time, so this keeps the lamp tracking the finger without
 * a growing backlog.
 */
function latestOnly<T>(send: (value: T) => Promise<void>): (value: T) => void {
  let inFlight = false;
  let queued: T | null = null;

  async function run(value: T): Promise<void> {
    inFlight = true;
    try {
      await send(value);
    } catch (err) {
      report(err);
    }
    inFlight = false;
    if (queued !== null) {
      const next = queued;
      queued = null;
      void run(next);
    }
  }

  return (value) => {
    if (inFlight) queued = value;
    else void run(value);
  };
}

// --- rendering what the lamp reports -------------------------------------------

/** Controls the user is holding right now; a pushed state must not yank them. */
const held = new Set<HTMLInputElement>();

/** The latest pushed state, for the controls that need to know what is on. */
let current: { lamp: LampJson | null; sun: SunJson | null } = { lamp: null, sun: null };
/** Which light was on most recently, so "on" from all-off brings back the right one. */
let lastLight: "rgb" | "sun" = "rgb";

function render(connected: boolean, lamp: LampJson | null, sun: SunJson | null): void {
  if (!connected || lamp === null) {
    setLink("lamp", "lamp away, reconnecting…");
    panel.dataset["on"] = "false";
    lampLabel.textContent = "away";
    return;
  }
  setLink("connected", "connected");
  current = { lamp, sun };

  // The disc shows whichever light is on. The lamp never runs both.
  const sunOn = sun?.on ?? false;
  if (sunOn) lastLight = "sun";
  else if (lamp.on) lastLight = "rgb";
  const hex = toHex(lamp.rgb);
  if (sunOn) {
    panel.style.setProperty("--lamp-color", sunColor(sun?.temperature ?? 128));
    panel.style.setProperty("--lamp-level", ((sun?.level ?? 16) / 16).toFixed(3));
    panel.dataset["on"] = "true";
    panel.dataset["mode"] = "sun";
    lampLabel.textContent = "room light";
  } else {
    panel.style.setProperty("--lamp-color", hex);
    panel.style.setProperty("--lamp-level", (lamp.brightness / 255).toFixed(3));
    panel.dataset["on"] = String(lamp.on);
    panel.dataset["mode"] = lamp.mode;
    lampLabel.textContent = lamp.on ? lamp.mode : "off";
  }

  if (!held.has(brightnessInput)) brightnessInput.value = String(lamp.brightness);
  brightnessValue.textContent = String(lamp.brightness);

  if (!held.has(colorInput)) colorInput.value = hex;
  colorValue.textContent = hex;

  for (const button of modeButtons.querySelectorAll<HTMLButtonElement>("button")) {
    button.classList.toggle("active", !sunOn && button.dataset["mode"] === lamp.mode);
  }

  sunToggle.setAttribute("aria-pressed", String(sunOn));
  if (sun?.temperature != null && !held.has(sunTempInput)) {
    sunTempInput.value = String(sun.temperature);
  }
  if (sun !== null) {
    if (!held.has(sunLevelInput)) sunLevelInput.value = String(sun.level);
    sunLevelValue.textContent = String(sun.level);
  }
}

function setLink(kind: "hub" | "lamp" | "connected", text: string): void {
  panel.dataset["link"] = kind;
  statusText.textContent = text;
}

/** What the ring looks like at a temperature: warm amber-white at 0, cool blue-white at 255. */
function sunColor(temperature: number): string {
  const warm: Rgb = [255, 180, 107];
  const cold: Rgb = [226, 238, 255];
  const t = temperature / 255;
  return toHex(warm.map((w, i) => Math.round(w + (cold[i]! - w) * t)) as Rgb);
}

// --- the controls --------------------------------------------------------------

function report(err: unknown): void {
  showToast(err instanceof Error ? err.message : String(err));
}

// The disc swaps between the two lights; it never turns the lamp off.
// The lamp itself switches the other light off when one comes on.
lampButton.addEventListener("click", () => {
  const sunOn = current.sun?.on ?? false;
  const rgbOn = current.lamp?.on ?? false;
  let next: HubRequest;
  if (sunOn) next = { type: "power", on: true };
  else if (rgbOn) next = { type: "sun", on: true };
  else next = lastLight === "sun" ? { type: "sun", on: true } : { type: "power", on: true };
  request(next).catch(report);
});

// The only control that turns the lamp off: whichever light is on, off it goes.
offButton.addEventListener("click", () => {
  if (current.sun?.on) request({ type: "sun", on: false }).catch(report);
  else if (current.lamp?.on) request({ type: "power", on: false }).catch(report);
});

const sendBrightness = latestOnly((value: number) => request({ type: "brightness", value }));
brightnessInput.addEventListener("input", () => {
  brightnessValue.textContent = brightnessInput.value;
  sendBrightness(Number(brightnessInput.value));
});

const sendColor = latestOnly(([r, g, b]: Rgb) => request({ type: "color", r, g, b }));
colorInput.addEventListener("input", () => {
  colorValue.textContent = colorInput.value;
  sendColor(fromHex(colorInput.value));
});
for (const swatch of document.querySelectorAll<HTMLButtonElement>(".swatch[data-rgb]")) {
  swatch.addEventListener("click", () => {
    const rgb = (swatch.dataset["rgb"] ?? "").split(",").map(Number);
    if (rgb.length === 3) sendColor(rgb as Rgb);
  });
}

sunToggle.addEventListener("click", () => {
  const isOn = sunToggle.getAttribute("aria-pressed") === "true";
  request({ type: "sun", on: !isOn }).catch(report);
});

const sendSunTemperature = latestOnly((value: number) =>
  request({ type: "sun_temperature", value }),
);
sunTempInput.addEventListener("input", () => {
  sendSunTemperature(Number(sunTempInput.value));
});

const sendSunLevel = latestOnly((value: number) => request({ type: "sun_level", value }));
sunLevelInput.addEventListener("input", () => {
  sunLevelValue.textContent = sunLevelInput.value;
  sendSunLevel(Number(sunLevelInput.value));
});

// While a finger is on a slider or the picker is open, pushed states
// update the numbers but leave the control where the user put it.
for (const slider of [brightnessInput, sunTempInput, sunLevelInput]) {
  slider.addEventListener("pointerdown", () => held.add(slider));
  for (const type of ["pointerup", "pointercancel"]) {
    slider.addEventListener(type, () => held.delete(slider));
  }
}
colorInput.addEventListener("focus", () => held.add(colorInput));
for (const type of ["change", "blur"]) {
  colorInput.addEventListener(type, () => held.delete(colorInput));
}

modeButtons.addEventListener("click", (event) => {
  const target = event.target as Element;
  const mode = target.closest("button")?.dataset["mode"];
  if (isModeName(mode)) request({ type: "mode", mode }).catch(report);
});

// --- small helpers -------------------------------------------------------------

function isModeName(value: string | undefined): value is ModeName {
  return MODE_NAMES.includes(value as ModeName);
}

function toHex([r, g, b]: Rgb): string {
  return `#${[r, g, b].map((v) => v.toString(16).padStart(2, "0")).join("")}`;
}

function fromHex(hex: string): Rgb {
  return [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16)) as Rgb;
}

let toastTimer: ReturnType<typeof setTimeout> | null = null;
function showToast(text: string): void {
  toast.textContent = text;
  toast.classList.add("show");
  if (toastTimer !== null) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 3200);
}

connect();
