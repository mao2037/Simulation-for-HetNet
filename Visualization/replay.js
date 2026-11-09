"use strict";

const data = window.TOY_REPLAY;
const byId = (id) => document.getElementById(id);
const canvas = byId("map");
const context = canvas.getContext("2d");
const play = byId("play");
const seek = byId("seek");
const speed = byId("speed");
const colors = {
  ink: "#152d32", ew: "#4f805c", ns: "#567da7",
  passenger: "#d63361", waiting: "#ab570c", onboard: "#ff8ca9",
};

let time = 0;
let playing = !matchMedia("(prefers-reduced-motion: reduce)").matches;
let lastFrame;
let pendingFrame;
let map;
const background = document.createElement("canvas");
const base = background.getContext("2d");

byId("city-size").textContent = data.size + " × " + data.size + " km";
byId("scenario-demand").textContent = data.demand + " passengers / h";
byId("network-detail").textContent =
  data.stations.length + " stations · " + data.routeCount + " operating routes · Replication 01";
seek.max = data.duration;

function project([x, y]) {
  return [map.x + x * map.scale, map.y + map.side - y * map.scale];
}

function dot(ctx, x, y, radius, fill, stroke, width = 1) {
  ctx.beginPath();
  ctx.arc(x, y, radius, 0, Math.PI * 2);
  ctx.fillStyle = fill;
  ctx.fill();
  if (stroke) {
    ctx.strokeStyle = stroke;
    ctx.lineWidth = width;
    ctx.stroke();
  }
}

function line(ctx, points, color, width) {
  ctx.beginPath();
  ctx.moveTo(...points[0]);
  for (const point of points.slice(1)) ctx.lineTo(...point);
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.stroke();
}

function drawBackground() {
  const { x, y, side, scale } = map;
  base.fillStyle = "#f9fbf8";
  base.fillRect(x, y, side, side);
  base.lineJoin = "round";
  base.lineCap = "round";
  base.font = '10px "Cascadia Mono", Consolas, monospace';
  base.fillStyle = "#526b6a";
  for (let km = 0; km <= data.size; km += 0.5) {
    const [gridX, gridY] = project([km, km]);
    line(base, [[gridX, y], [gridX, y + side]], "#dce7e1", 1);
    line(base, [[x, gridY], [x + side, gridY]], "#dce7e1", 1);
    base.textAlign = "center";
    base.fillText(String(km), gridX, y + side + 17);
    base.textAlign = "right";
    base.fillText(String(km), x - 9, gridY + 3);
  }
  base.strokeStyle = "#a9beb7";
  base.lineWidth = 1;
  base.strokeRect(x, y, side, side);

  for (const segment of data.lines) {
    const color = segment.family === "EW" ? colors.ew : colors.ns;
    line(base, segment.points.map(project), color, segment.kind === "main" ? 2.05 : 1.45);
  }
  for (const station of data.stations) {
    dot(base, ...project(station), 3, "#fff", colors.ink);
  }

  base.fillStyle = "#526b6a";
  base.textAlign = "left";
  base.fillText("km", x + side + 8, y + side + 17);
  const barY = y + side + 30;
  const barWidth = 0.5 * scale;
  line(base, [[x, barY - 2], [x, barY], [x + barWidth, barY], [x + barWidth, barY - 2]], colors.ink, 1);
  base.fillText("500 m", x + barWidth + 7, barY + 3);
  base.textAlign = "center";
  base.fillText("N ↑", x + side - 10, y - 10);
}

function resize() {
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  const ratio = Math.min(devicePixelRatio || 1, 3);
  const side = Math.min(width - 74, height - 65);
  map = { x: (width - side) / 2, y: (height - side - 16) / 2, side, scale: side / data.size };
  for (const layer of [canvas, background]) {
    layer.width = Math.round(width * ratio);
    layer.height = Math.round(height * ratio);
  }
  base.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  drawBackground();
  draw();
}

function interpolate(a, b, start, end) {
  const fraction = end > start ? Math.max(0, Math.min(1, (time - start) / (end - start))) : 1;
  return [a[0] + (b[0] - a[0]) * fraction, a[1] + (b[1] - a[1]) * fraction];
}

function busPose(bus) {
  const frames = data.paths[bus.path];
  const relative = time - bus.start;
  let next = 1;
  while (next < frames.length - 1 && relative >= frames[next][0]) next++;
  const [start, ax, ay] = frames[next - 1];
  const [end, bx, by] = frames[next];
  const [x, y] = project(interpolate([ax, ay], [bx, by], bus.start + start, bus.start + end));
  // During dwell, point the bus toward the next non-coincident path vertex.
  const heading = frames.slice(next).find((frame) => Math.hypot(frame[1] - ax, frame[2] - ay) > 1e-8);
  const angle = heading ? Math.atan2(ay - heading[2], heading[1] - ax) : 0;
  return { x, y, angle, riders: 0 };
}

function locatePassenger(passenger) {
  const { start, ready, origin, destination, station, egress, finish } = passenger;
  if (station === null) return { point: interpolate(origin, destination, start, finish) };
  if (time < ready) return { point: interpolate(origin, data.stations[station], start, ready) };
  let waitingAt = station;
  for (const ride of passenger.rides) {
    if (time < ride.board) break;
    if (ride.alight === null || time < ride.alight) return { bus: ride.trip };
    waitingAt = ride.station;
  }
  if (egress !== null && time >= egress) {
    return { point: interpolate(data.stations[waitingAt], destination, egress, finish) };
  }
  return { waitingAt };
}

function drawBus({ x, y, angle, riders }) {
  const width = Math.max(21, 10 + riders * 5);
  context.save();
  context.translate(x, y);
  context.rotate(angle);
  context.beginPath();
  context.roundRect(-width / 2, -5.5, width, 11, 3);
  context.fillStyle = colors.ink;
  context.fill();
  context.strokeStyle = "#fff";
  context.lineWidth = 1.1;
  context.stroke();
  context.fillStyle = "#aee4d8";
  context.fillRect(width / 2 - 4.5, -2.7, 2.1, 5.4);
  for (let rider = 0; rider < riders; rider++) {
    dot(context, -width / 2 + 4.5 + rider * 5, 0, 1.85, colors.onboard);
  }
  context.restore();
}

function draw() {
  if (!map) return;
  context.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
  context.drawImage(background, 0, 0, canvas.clientWidth, canvas.clientHeight);
  const buses = new Map();
  const walkers = [];
  const waiting = new Map();
  let started = 0;
  let completed = 0;
  let active = 0;
  for (const bus of data.buses) {
    if (bus.start <= time && time < bus.finish) buses.set(bus.id, busPose(bus));
  }
  for (const passenger of data.passengers) {
    if (passenger.start > time) continue;
    if (passenger.start >= 0) started++;
    if (passenger.finish !== null && passenger.finish <= time) {
      if (passenger.start >= 0) completed++;
      continue;
    }
    active++;
    const location = locatePassenger(passenger);
    if (location.bus) {
      buses.get(location.bus).riders++;
    } else if (location.point) {
      walkers.push(location.point);
    } else {
      const station = location.waitingAt;
      waiting.set(station, (waiting.get(station) || 0) + 1);
    }
  }
  for (const bus of buses.values()) drawBus(bus);
  for (const [station, count] of waiting) {
    const [x, y] = project(data.stations[station]);
    for (let i = 0; i < count; i++) {
      const ring = Math.floor(i / 6);
      const angle = Math.PI / 4 + i * Math.PI / 3;
      const radius = 8 + 7 * ring;
      dot(context, x + Math.cos(angle) * radius, y + Math.sin(angle) * radius,
          3.15, "#ffdfac", colors.waiting, 1.05);
    }
  }
  for (const point of walkers) dot(context, ...project(point), 3.5, colors.passenger, "#fff", 1.15);

  const seconds = Math.floor(time);
  const label = String(Math.floor(seconds / 60)).padStart(2, "0")
    + ":" + String(seconds % 60).padStart(2, "0");
  updateText("clock", label);
  seek.value = time;
  seek.setAttribute("aria-valuetext", label);
  updateText("buses", buses.size);
  updateText("passengers", active);
  updateText("started", started);
  updateText("completed", completed);
}

function updateText(id, value) {
  const element = byId(id);
  if (element.textContent !== String(value)) element.textContent = value;
}

function tick(now) {
  pendingFrame = undefined;
  if (!playing) return;
  if (lastFrame !== undefined) {
    time = (time + (now - lastFrame) * Number(speed.value) / 1000) % data.duration;
  }
  lastFrame = now;
  draw();
  pendingFrame = requestAnimationFrame(tick);
}

function setPlaying(value) {
  playing = value;
  lastFrame = undefined;
  const label = playing ? "Pause replay" : "Play replay";
  play.setAttribute("aria-label", label);
  play.title = label;
  byId("play-icon").setAttribute("d", playing ? "M8 5h3v14H8zm5 0h3v14h-3z" : "M8 5l11 7-11 7z");
  if (pendingFrame !== undefined) {
    cancelAnimationFrame(pendingFrame);
    pendingFrame = undefined;
  }
  if (playing) {
    if (time >= data.duration) time = 0;
    pendingFrame = requestAnimationFrame(tick);
  }
}

play.addEventListener("click", () => setPlaying(!playing));
byId("restart").addEventListener("click", () => {
  time = 0;
  lastFrame = undefined;
  draw();
});
seek.addEventListener("pointerdown", () => setPlaying(false));
seek.addEventListener("input", () => {
  setPlaying(false);
  time = Number(seek.value);
  draw();
});
speed.addEventListener("change", () => { lastFrame = undefined; });
document.addEventListener("visibilitychange", () => { lastFrame = undefined; });

new ResizeObserver(resize).observe(canvas);
resize();
setPlaying(playing);
