// web/abr.js without a browser: a fake XHR playing axios' part (reads responseText in onloadend).
//   node web/tests/abr.test.js
'use strict';
const fs = require('fs'), vm = require('vm'), assert = require('assert'), path = require('path');
const src = fs.readFileSync(path.join(__dirname, '..', 'abr.js'), 'utf8');
const ID = 'aaa21d2f6870ec2f501b705d0a510e50';

function environment(answers) {
  class XHR {
    constructor() { this.listeners = []; this.readyState = 0; this.status = 0; this.responseType = ''; }
    open(m, u) { this.m = m; this.u = u; }
    setRequestHeader() {}
    addEventListener(ev, f) { if (ev === 'readystatechange') this.listeners.push(f); }
    send() {
      const r = answers(this.m, this.u);
      Object.defineProperty(this, 'responseText', { configurable: true, value: r.text, writable: true });
      this.status = r.status; this.readyState = 4;
      this.listeners.forEach(f => f());
      if (this.onload) this.onload();
      if (this.onloadend) this.onloadend();
    }
  }
  const ctx = { XMLHttpRequest: XHR, JSON, String, Date, console: { info() {} },
    location: { hostname: 'media.example.org' }, document: { addEventListener() {} },
    setInterval: f => { f(); return 1; }, clearInterval() {},
    ApiClient: { accessToken: () => 'tok123', serverAddress: () => 'https://media.example.org' } };
  ctx.window = ctx;
  vm.runInNewContext(src, ctx);
  return ctx;
}
const transcode = { PlaySessionId: 'ps1', MediaSources: [{ Id: ID, SupportsDirectPlay: false, SupportsDirectStream: false,
  TranscodingUrl: '/videos/x/master.m3u8?...', TranscodingSubProtocol: 'hls',
  MediaStreams: [{ Type: 'Subtitle', Index: 0, IsTextSubtitleStream: true, DeliveryMethod: 'External', DeliveryUrl: '/orig' },
                 { Type: 'Subtitle', Index: 4, IsTextSubtitleStream: true, DeliveryMethod: 'Hls' }] }] };
function ask(ctx, id) {
  const x = new ctx.XMLHttpRequest(); let read;
  x.onloadend = () => { read = x.responseText; };          // like axios
  x.open('POST', 'https://media.example.org/Items/' + id + '/PlaybackInfo?UserId=u');
  x.send('{}');
  return JSON.parse(read);
}
const list = (m, u) => u.endsWith('/ondemand/api/abr') ? { status: 200, text: JSON.stringify({ items: [ID] }) }
                                                       : { status: 200, text: JSON.stringify(transcode) };
// 1) item with a ladder and a transcode → the ladder
let ctx = environment(list), d = ask(ctx, ID);
assert.strictEqual(d.MediaSources[0].TranscodingUrl, '/ondemand/abr/t/tok123/' + ID + '/master.m3u8?PlaySessionId=ps1');
assert.strictEqual(d.MediaSources[0].TranscodingSubProtocol, 'hls');
assert.strictEqual(d.MediaSources[0].MediaStreams[0].DeliveryUrl, '/orig');                  // external untouched
assert.strictEqual(d.MediaSources[0].MediaStreams[1].DeliveryMethod, 'External');            // Hls → External
assert.ok(d.MediaSources[0].MediaStreams[1].DeliveryUrl.startsWith('/Videos/' + ID + '/' + ID + '/Subtitles/4/0/Stream.vtt'));
// 2) item without a ladder → untouched
d = ask(ctx, 'b'.repeat(32));
assert.strictEqual(d.MediaSources[0].TranscodingUrl, '/videos/x/master.m3u8?...');
// 3) Direct Play → untouched
const direct = JSON.parse(JSON.stringify(transcode)); direct.MediaSources[0].SupportsDirectPlay = true;
ctx = environment((m, u) => u.endsWith('/ondemand/api/abr') ? list(m, u) : { status: 200, text: JSON.stringify(direct) });
d = ask(ctx, ID);
assert.strictEqual(d.MediaSources[0].TranscodingUrl, '/videos/x/master.m3u8?...');
// 4) the list fails → untouched
ctx = environment((m, u) => u.endsWith('/ondemand/api/abr') ? { status: 401, text: '{}' } : { status: 200, text: JSON.stringify(transcode) });
d = ask(ctx, ID);
assert.strictEqual(d.MediaSources[0].TranscodingUrl, '/videos/x/master.m3u8?...');
console.log('ok: 4 scenarios');
