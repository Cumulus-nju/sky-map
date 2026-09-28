const http = require('http'), fs = require('fs');
const get = (path) => new Promise((res, rej) => {
  http.get({host:'127.0.0.1', port:9222, path}, r => { let d=''; r.on('data',c=>d+=c); r.on('end',()=>res(d)); }).on('error', rej);
});
(async () => {
  const targets = JSON.parse(await get('/json/list'));
  const page = targets.find(t => t.type === 'page');
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0; const pend = new Map();
  const send = (m, p = {}) => new Promise(r => { const i = ++id; pend.set(i, r); ws.send(JSON.stringify({id:i, method:m, params:p})); });
  ws.addEventListener('message', ev => { const j = JSON.parse(ev.data); if (j.id && pend.has(j.id)) { pend.get(j.id)(j); pend.delete(j.id); } });
  await new Promise(r => ws.addEventListener('open', r));
  await send('Page.enable'); await send('Runtime.enable');
  await send('Page.navigate', {url: 'https://azzjin9anwdbyykyd3dyh6.streamlit.app/'});
  // 轮询等 Streamlit 真正渲染出来
  let txt = '';
  for (let i = 0; i < 24; i++) {
    await new Promise(r => setTimeout(r, 5000));
    const r = await send('Runtime.evaluate', {
      expression: `(() => { const el = document.querySelector('section.main, [data-testid="stAppViewContainer"], .stApp'); return el ? el.innerText : ''; })()`,
      returnByValue: true});
    txt = (r.result && r.result.result && r.result.result.value) || '';
    if (txt.trim().length > 40) { console.log(`(第 ${i+1} 次轮询拿到内容)`); break; }
  }
  console.log('=== 渲染文本（前 3000 字）===');
  console.log(txt.trim().slice(0, 3000) || '(仍然为空)');
  console.log('');
  console.log('=== 关键判断 ===');
  console.log('Supabase 云端存储 :', /Supabase/i.test(txt));
  console.log('本地文件存储      :', /本地文件存储/.test(txt));
  console.log('报错              :', /Traceback|Exception|Error|错误/.test(txt));
  const shot = await send('Page.captureScreenshot', {format: 'png'});
  if (shot.result && shot.result.data) {
    fs.writeFileSync('C:\\Users\\Administrator\\.dsh\\sky_map\\_live.png', Buffer.from(shot.result.data, 'base64'));
    console.log('\n截图已保存: _live.png');
  }
  ws.close(); process.exit(0);
})().catch(e => { console.log('ERR ' + e.message); process.exit(1); });
