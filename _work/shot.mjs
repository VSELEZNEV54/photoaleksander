// Скриншоты через chrome-headless-shell + CDP (без зависимостей, Node 22+).
// node _work/shot.mjs <url> <width> <height> <outPrefix> [selectorOrY ...]
// Аргументы после outPrefix: число — scrollY, иначе CSS-селектор (прокрутка к нему).
// Префикс "click:" — клик по селектору перед снимком; "hover:" — наведение.
// Покадрово (анимация загрузки): "frames:0,150,400,800" — снимки через N мс после последнего перехода
// (если это первый шаг — без обычной паузы 3,5 с после загрузки); "nav:<url>" — перейти в той же сессии
// (sessionStorage сохраняется) и отсчитывать frames от этого перехода; "clicknav:<селектор>" — кликнуть по ссылке
// (переход страницы) и отсчитывать frames от клика. "wait:<мс>" — просто подождать. "eval:<js>" — выполнить выражение
// в странице и напечатать результат. "reduce" — эмулировать prefers-reduced-motion (для следующих переходов), "back" — history.back().
// У каждого снимка печатается фактическое время съёмки (@мс от перехода/клика).
import { spawn } from 'node:child_process';
import { writeFileSync } from 'node:fs';
import { homedir } from 'node:os';

const BIN = homedir() + '/Library/Caches/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-mac-arm64/chrome-headless-shell';
const [url, W, H, out, ...steps] = process.argv.slice(2);
const port = 9300 + Math.floor(Math.random() * 500);
const chrome = spawn(BIN, [`--remote-debugging-port=${port}`, '--hide-scrollbars', '--no-first-run', '--user-data-dir=/tmp/chs-' + port, 'about:blank'], { stdio: 'ignore' });
const sleep = ms => new Promise(r => setTimeout(r, ms));

let ws, id = 0; const pending = new Map();
async function connect() {
  for (let i = 0; i < 50; i++) {
    try { const r = await fetch(`http://127.0.0.1:${port}/json/list`); const t = (await r.json()).find(x => x.type === 'page'); if (t) return t.webSocketDebuggerUrl; } catch {}
    await sleep(150);
  }
  throw new Error('no chrome');
}
const send = (method, params = {}) => new Promise((res, rej) => { const i = ++id; pending.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method, params })); });
const evalJs = async expr => (await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true })).result.value;

try {
  ws = new WebSocket(await connect());
  await new Promise(r => ws.addEventListener('open', r));
  ws.addEventListener('message', e => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.rej(new Error(m.error.message)) : p.res(m.result); } });
  const mobile = +W < 768;
  await send('Page.enable'); await send('Runtime.enable');
  await send('Emulation.setDeviceMetricsOverride', { width: +W, height: +H, deviceScaleFactor: 1, mobile });
  if (mobile) await send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 5 });
  let t0 = Date.now();
  await send('Page.navigate', { url });
  if (!(steps[0] || '').startsWith('frames:')) await sleep(3500);
  let n = 0;
  // сразу после Page.navigate страница ещё не подключена («Not attached to an active page») — повторяем, пока не подключится
  // в момент смены документа (переход по ссылке) вызов может повиснуть без ответа — ждём не дольше 1,2 с и повторяем
  const capture = async () => { for (let i = 0; ; i++) { try { return await Promise.race([send('Page.captureScreenshot', { format: 'jpeg', quality: 80 }), sleep(450).then(() => { throw new Error('not attached: timeout'); })]); } catch (e) { if (i > 40 || !/attached/i.test(e.message)) throw e; await sleep(25); } } };
  const snap = async (tag) => { const { data } = await capture(); const f = `${out}-${String(n++).padStart(2, '0')}-${tag}.jpg`; writeFileSync(f, Buffer.from(data, 'base64')); console.log(f, '@' + (Date.now() - t0) + 'ms'); };
  if (!steps.length) await snap('top');
  for (const s of steps) {
    if (s.startsWith('frames:')) {
      for (const ms of s.slice(7).split(',').map(Number)) { const dt = ms - (Date.now() - t0); if (dt > 0) await sleep(dt); await snap('t' + ms); }
      continue;
    }
    if (s.startsWith('nav:')) { t0 = Date.now(); await send('Page.navigate', { url: s.slice(4) }); continue; }
    if (s.startsWith('clicknav:')) { t0 = Date.now(); await evalJs(`(()=>{const e=document.querySelector(${JSON.stringify(s.slice(9))}); if(e){e.click(); return 1} return 0})()`); continue; }
    if (s.startsWith('wait:')) { await sleep(+s.slice(5)); continue; }
    if (s === 'reduce') { await send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-reduced-motion', value: 'reduce' }] }); continue; }
    if (s === 'back') { t0 = Date.now(); await evalJs('setTimeout(function(){history.back()},0)'); continue; }
    if (s.startsWith('eval:')) { console.log('eval', JSON.stringify(await evalJs(s.slice(5)))); continue; }
    if (s.startsWith('click:')) { await evalJs(`(()=>{const e=document.querySelector(${JSON.stringify(s.slice(6))}); if(e){e.click(); return 1} return 0})()`); await sleep(1200); await snap('click'); continue; }
    if (s.startsWith('hover:')) {
      const r = await evalJs(`(()=>{const e=document.querySelector(${JSON.stringify(s.slice(6))}); if(!e) return null; e.scrollIntoView({block:'center',behavior:'instant'}); const b=e.getBoundingClientRect(); return [b.x+b.width/2,b.y+b.height/2]})()`);
      if (r) { await sleep(600); for (let k = 0; k < 6; k++) { await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: r[0] - 60 + k * 20, y: r[1] }); await sleep(120); } await sleep(900); }
      await snap('hover'); continue;
    }
    if (s === 'full') { const h = await evalJs('document.documentElement.scrollHeight'); console.log('height', h); continue; }
    const js = /^\d+$/.test(s)
      ? `window.scrollTo({top:${s},behavior:'instant'})`
      : `(()=>{const e=document.querySelector(${JSON.stringify(s)}); if(e) window.scrollTo({top:e.getBoundingClientRect().top+scrollY-8,behavior:'instant'})})()`;
    await evalJs(js); await sleep(1600);
    await snap(s.replace(/[^a-z0-9]+/gi, '_').slice(0, 30));
  }
  console.log('scrollWidth', await evalJs('document.documentElement.scrollWidth'), 'innerWidth', await evalJs('innerWidth'));
} catch (e) { console.error('ERR', e.message); }
finally { try { ws && ws.close(); } catch {} chrome.kill('SIGKILL'); process.exit(0); }
