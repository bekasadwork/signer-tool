#!/usr/bin/env python3
"""Local helper that signs pasted content with the E-IMZO desktop app and timestamps the PKCS#7.

Run `python3 eimzo_signer.py`, open http://localhost:8765, see README.md.

The page talks to the E-IMZO app itself (websocket on 127.0.0.1). This server only serves the page and forwards
the one call the browser cannot make cross-origin: the PKCS#7 timestamp. Standard library only.
"""
import argparse
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = r"""<!doctype html>
<meta charset="utf-8">
<title>E-IMZO signer</title>
<body style="font-family:sans-serif;max-width:960px;margin:20px auto;padding:0 16px">
<h3>E-IMZO signer</h3>
<p>Key: <select id="keys"></select> <button onclick="run(listKeys)">Reload keys</button></p>
<p>Content to sign (paste it exactly as the content endpoint returned it)<br>
  <textarea id="content" rows="8" style="width:100%"></textarea></p>
<p><button onclick="run(sign)">Sign and timestamp</button></p>
<p>PKCS7 (attached, no timestamp) <button onclick="copy('plain')">Copy</button><br>
  <textarea id="plain" rows="5" style="width:100%" readonly></textarea></p>
<p>PKCS7 with timestamp <button onclick="copy('stamped')">Copy</button><br>
  <textarea id="stamped" rows="5" style="width:100%" readonly></textarea></p>
<pre id="log" style="color:#555;white-space:pre-wrap;word-break:break-all"></pre>
<script>
const WS_URL = 'ws://127.0.0.1:64646/service/cryptapi';
// Public localhost keys, same defaults as @hujjatuz/eimzo-client. They only work on http://localhost or 127.0.0.1.
const API_KEYS = [
  'localhost', '96D0C1491615C82B9A54D9989779DF825B690748224C2B04F500F370D51827CE2644D8D4A82C18184D73AB8530BB8ED537269603F61DB0D03D2104ABF789970B',
  '127.0.0.1', 'A7BCFA5D490B351BE0754130DF03A068F855DB4333D43921125B9CF2670EF6A40370C646B90401955E1F7BC9CDBF59CE0B2C5467D820BE189C845D0B79CFC96F',
];
const $ = id => document.getElementById(id);
const log = m => $('log').textContent += m + '\n';
const copy = id => navigator.clipboard.writeText($(id).value);
const run = step => step().catch(e => log('ERROR ' + e.message));
let certs = [], keyIds = {};

// One socket per call, like CAPIWS in e-imzo.js.
function eimzo(msg) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(WS_URL);
    ws.onerror = () => reject(new Error('E-IMZO is not reachable at ' + WS_URL + ' (is the app running?)'));
    ws.onopen = () => ws.send(JSON.stringify(msg));
    ws.onmessage = e => {
      ws.close();
      const d = JSON.parse(e.data);
      d.success === false ? reject(new Error('E-IMZO: ' + (d.reason || e.data))) : resolve(d);
    };
  });
}

async function listKeys() {
  await eimzo({ name: 'apikey', arguments: API_KEYS });
  certs = (await eimzo({ plugin: 'pfx', name: 'list_all_certificates' })).certificates;
  $('keys').replaceChildren(...certs.map((c, i) =>
    new Option(`${(c.alias.match(/(?:^|,)cn=([^,]*)/i) || [])[1] || c.name} (${c.disk}${c.path}/${c.name})`, i)));
  log(`found ${certs.length} key(s)`);
}

async function sign() {
  const i = $('keys').value, c = certs[i], content = $('content').value;
  if (!c) throw new Error('no key selected');
  if (!content) throw new Error('paste the content to sign first');
  $('plain').value = $('stamped').value = '';

  try {
    // load_key makes E-IMZO ask for the password; the keyId is reused until it fails.
    keyIds[i] ??= (await eimzo({ plugin: 'pfx', name: 'load_key', arguments: [c.disk, c.path, c.name, c.alias] })).keyId;
    // the text is signed as UTF-8, untrimmed; "no" = attached (the backend verifies attached PKCS7 only)
    const data64 = btoa(Array.from(new TextEncoder().encode(content), b => String.fromCharCode(b)).join(''));
    $('plain').value = (await eimzo({ plugin: 'pkcs7', name: 'create_pkcs7', arguments: [data64, keyIds[i], 'no'] })).pkcs7_64;
  } catch (e) { delete keyIds[i]; throw e; }
  log(`pkcs7: ${$('plain').value.length} chars`);

  // an upstream error keeps its status and body so the real reason is visible
  const r = await fetch('/api/timestamp', { method: 'POST', body: $('plain').value });
  if (!r.ok) throw new Error(`timestamp -> HTTP ${r.status}: ${(await r.text()).slice(0, 2000)}`);
  const stamped = await r.json();
  if (!stamped.pkcs7b64) throw new Error('timestamp refused: ' + JSON.stringify(stamped).slice(0, 2000));
  $('stamped').value = stamped.pkcs7b64;
  log(`timestamped pkcs7: ${stamped.pkcs7b64.length} chars`);
}

run(listKeys);
</script>
"""


class Handler(BaseHTTPRequestHandler):
    eimzo_url = ''

    def do_GET(self):
        if self.path == '/':
            self.reply(200, 'text/html; charset=utf-8', PAGE.encode())
        else:
            self.reply(404, 'text/plain', b'not found')

    def do_POST(self):
        if self.path != '/api/timestamp':
            return self.reply(404, 'text/plain', b'not found')
        # raw base64 body under a JSON content type, the same request document-service sends
        url = self.eimzo_url + '/frontend/timestamp/pkcs7'
        body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        try:
            request = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(request, timeout=60) as res:
                self.reply(res.status, res.headers.get('Content-Type', 'application/octet-stream'), res.read())
        except urllib.error.HTTPError as e:
            self.reply(e.code, e.headers.get('Content-Type', 'text/plain'), e.read())
        except OSError as e:  # URLError and timeouts: DNS, refused connection, TLS
            self.reply(502, 'text/plain; charset=utf-8', f'{url}: {e}'.encode())

    def reply(self, status, content_type, body):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--eimzo-url', default='https://hujjat.uz/mobile-id', help='E-IMZO server (default: %(default)s)')
    args = parser.parse_args()
    Handler.eimzo_url = args.eimzo_url.rstrip('/')

    # loopback only: the forwarding endpoint must not be reachable from the network
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'E-IMZO signer: http://localhost:{args.port}  (e-imzo: {Handler.eimzo_url})', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
