// Reference Worker Protocol client in Node.js (22+, built-in WebSocket).
// Shows how a game server in ANY language plugs into HGS: HGS does login, rooms and
// security; this program only implements game rules. Protocol: docs/architecture/06-protocol.md §6.4
"use strict";

const ws = new WebSocket(process.env.HGS_WORKER_URL);
const send = (t, d, extra = {}) => ws.send(JSON.stringify({ v: 1, t, d, ...extra }));
const reply = (id, d) => send("w.reply", d, { re: id });
const names = new Map(); // player id -> name

ws.onopen = () => send("w.hello", { token: process.env.HGS_WORKER_TOKEN, game_id: process.env.HGS_GAME_ID, api: 1, sdk: "node-example/1.0" });
ws.onclose = () => process.exit(3); // never outlive HGS
ws.onmessage = (ev) => {
  const { t, d = {}, id } = JSON.parse(ev.data);
  switch (t) {
    case "w.welcome": console.log("node worker ready"); reply(id, { ready: true }); break;
    case "w.ping": reply(id, {}); break;
    case "w.player.join": names.set(d.player.id, d.player.name); reply(id, { accept: true }); break;
    case "w.player.leave": names.delete(d.player_id); break;
    case "w.player.event":
      if (d.e === "chat") send("w.send", { room_id: d.room_id, e: "chat", d: { from: names.get(d.player_id), text: String(d.d).slice(0, 500) } });
      break;
    case "w.save": reply(id, { supported: false }); break;
    case "w.shutdown": ws.close(); break;
  }
};
