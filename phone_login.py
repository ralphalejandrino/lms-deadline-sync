#!/usr/bin/env python3
"""Drive the Blackboard SSO login from a phone.

Launches the SAVED Blackboard profile headless on RE, then serves a tiny page
that streams screenshots and forwards taps / typing back into the real browser.

Everything runs on ONE asyncio loop. Playwright's sync API is NOT thread-safe:
a ThreadingHTTPServer returns empty screenshots with no error, which looks
exactly like a broken browser. Do not "simplify" this back to threads.

  cd ~/vault/school/assets/lms
  ./lms.sh env >/dev/null                     # confirms venv + LD_LIBRARY_PATH
  LD_LIBRARY_PATH=$HOME/pwlibs/_extracted/usr/lib/x86_64-linux-gnu \
      $HOME/CRM/.venv/bin/python phone_login.py &
  tailscale serve --bg --https=8444 3006

Then open https://<your-machine>.<your-tailnet>.ts.net:8443 on the phone (tailnet only).
No X server, no sudo, no VNC. Ctrl-C to stop; cookies persist in the profile.
"""
import asyncio, json, pathlib, time
from playwright.async_api import async_playwright

PROFILE = pathlib.Path.home() / ".local/share/ub-blackboard-profile"
STATE   = PROFILE / "storage_state.json"
PORT, W, H = 3006, 1280, 2000
START = "https://blackboard.ubaguio.edu/"

PAGE = ("""<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>RE browser</title>
<style>body{margin:0;background:#111;color:#eee;font:14px system-ui}
#s{width:100%;display:block}
#bar{display:flex;gap:6px;padding:8px;position:sticky;top:0;background:#111;flex-wrap:wrap;z-index:9}
input{flex:1;min-width:130px;padding:10px;font-size:16px;border-radius:6px;border:1px solid #444;background:#222;color:#eee}
button{padding:10px 13px;font-size:15px;border-radius:6px;border:0;background:#3a6;color:#fff}
button.g{background:#555}#st{padding:0 8px;font-size:12px;color:#888}</style>
<div id=bar>
<input id=t placeholder="type, then Send" autocapitalize=off autocorrect=off spellcheck=false>
<button onclick="send()">Send</button>
<button class=g onclick="key('Enter')">Enter</button>
<button class=g onclick="key('Tab')">Tab</button>
<button class=g onclick="nav()">Go</button>
</div><div id=st>connecting...</div><img id=s>
<script>
const s=document.getElementById('s'),st=document.getElementById('st');
let busy=false;
async function tick(){
 if(!busy){busy=true;
  try{const r=await fetch('/shot?'+Date.now());
   if(r.ok){const b=await r.blob(); if(b.size>0){const u=URL.createObjectURL(b);
     const old=s.src; s.src=u; if(old.startsWith('blob:'))URL.revokeObjectURL(old);
     st.textContent=((b.size/1024)|0)+' KB  -  tap the image to click';}}
  }catch(e){st.textContent='reconnecting...';}
  busy=false;}
 setTimeout(tick,800);}
s.onclick=async e=>{const r=s.getBoundingClientRect();
 const x=Math.round((e.clientX-r.left)/r.width*__W__), y=Math.round((e.clientY-r.top)/r.height*__H__);
 st.textContent='click '+x+','+y;
 await fetch('/click',{method:'POST',body:JSON.stringify({x,y})});};
async function send(){const el=document.getElementById('t');
 await fetch('/type',{method:'POST',body:JSON.stringify({text:el.value})});el.value='';}
async function key(k){await fetch('/key',{method:'POST',body:JSON.stringify({key:k})});}
async function nav(){const u=prompt('URL','https://blackboard.ubaguio.edu/');
 if(u)await fetch('/nav',{method:'POST',body:JSON.stringify({url:u})});}
tick();
</script>""".replace("__W__", str(W)).replace("__H__", str(H))).encode()

page = None


async def handle(reader, writer):
    try:
        line = await reader.readline()
        if not line:
            return
        parts = line.decode("latin1").split()
        if len(parts) < 2:
            return
        method, path = parts[0], parts[1]

        clen = 0
        while True:
            h = await reader.readline()
            if h in (b"\r\n", b"\n", b""):
                break
            k, _, v = h.decode("latin1").partition(":")
            if k.strip().lower() == "content-length":
                clen = int(v.strip())
        body = await reader.readexactly(clen) if clen else b""

        async def reply(code, ctype, data):
            writer.write(
                f"HTTP/1.1 {code}\r\nContent-Type: {ctype}\r\n"
                f"Content-Length: {len(data)}\r\nCache-Control: no-store\r\n"
                f"Connection: close\r\n\r\n".encode() + data)
            await writer.drain()

        if method == "GET" and path.startswith("/shot"):
            try:
                png = await page.screenshot(type="png")
            except Exception as e:
                print("shot error:", repr(e)[:160], flush=True)
                png = b""
            await reply("200 OK", "image/png", png)
        elif method == "POST":
            d = json.loads(body or b"{}")
            try:
                if path == "/click":
                    await page.mouse.click(d["x"], d["y"])
                elif path == "/type":
                    await page.keyboard.type(d["text"], delay=25)
                elif path == "/key":
                    await page.keyboard.press(d["key"])
                elif path == "/save":
                    await page.context.storage_state(path=str(STATE))
                    print("saved on request ->", STATE, flush=True)
                elif path == "/nav":
                    await page.goto(d["url"], wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                print("action error:", repr(e)[:160], flush=True)
            await reply("200 OK", "application/json", b'{"ok":true}')
        else:
            await reply("200 OK", "text/html; charset=utf-8", PAGE)
    except Exception as e:
        print("conn error:", repr(e)[:120], flush=True)
    finally:
        try:
            writer.close()
        except Exception:
            pass


def _extend_session_cookies():
    """Give Blackboard's session-only cookies a real expiry.

    🔴 lms.sh DROPS any cookie whose expires is -1, so a freshly saved
    storage_state.json loads as "injected 2/8" and the sweep still 401s -- a
    live session that looks identical to a dead one. lms.sh's own cdp path
    rewrites these to +7d; do the same here or the export is worthless.
    Measured 2026-08-27: 6 of 8 Blackboard cookies are session-only.
    """
    try:
        d = json.loads(STATE.read_text())
        exp = time.time() + 7 * 86400
        n = 0
        for c in d.get("cookies", []):
            if ("blackboard" in c["domain"] or "ubaguio" in c["domain"]) \
                    and c.get("expires", -1) in (-1, None):
                c["expires"] = exp
                n += 1
        if n:
            STATE.write_text(json.dumps(d))
    except Exception as e:
        print("extend-cookies:", repr(e)[:120], flush=True)


async def autosave(ctx):
    """Persist the session to storage_state.json the moment it goes live.

    🔴 Blackboard's cookies are SESSION-ONLY. They exist only inside the running
    browser -- closing it discards the login, and an export attempted afterwards
    reads a profile that is already 401. A login was lost exactly this way on
    2026-08-27. So poll while the browser is UP and write on every 200.
    """
    was = False
    while True:
        await asyncio.sleep(15)
        try:
            r = await page.evaluate(
                """async () => { try { const x = await fetch(
                   'https://blackboard.ubaguio.edu/learn/api/public/v1/users/me',
                   {credentials:'include'}); return x.status; } catch(e) { return 0; } }""")
            if r == 200:
                await ctx.storage_state(path=str(STATE))
                _extend_session_cookies()
                if not was:
                    print(f"AUTHENTICATED -- session saved to {STATE}", flush=True)
                was = True
            elif was:
                print("session went 401", flush=True)
                was = False
        except Exception as e:
            print("autosave:", repr(e)[:100], flush=True)


async def main():
    global page
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), headless=True,
            viewport={"width": W, "height": H},
            args=["--disable-blink-features=AutomationControlled"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(START, wait_until="domcontentloaded", timeout=60000)
        saver = asyncio.create_task(autosave(ctx))
        srv = await asyncio.start_server(handle, "127.0.0.1", PORT)
        print(f"ready on 127.0.0.1:{PORT}", flush=True)
        try:
            async with srv:
                await srv.serve_forever()
        finally:
            saver.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("stopped")
